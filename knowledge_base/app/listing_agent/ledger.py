"""业务台账。

检查点负责让流程停在确认那一步还能接着跑。
这张表负责另一件事：同一次确认不能写两遍，结果不明时留下人工核对。
默认用 SQLite 文件，本机不用再开一个数据库。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT,
                idempotency_key TEXT UNIQUE,
                user_id TEXT,
                role TEXT,
                platform TEXT,
                query TEXT,
                status TEXT,
                sku TEXT,
                clarify_question TEXT,
                candidates_json TEXT,
                draft_json TEXT,
                draft_version INTEGER,
                validation_json TEXT,
                retrieval_json TEXT,
                evidence_json TEXT,
                receipt_json TEXT,
                cancelled INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                event TEXT,
                data_json TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS receipts (
                idempotency_key TEXT PRIMARY KEY,
                task_id TEXT,
                status TEXT,
                receipt_json TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS listings (
                listing_id TEXT PRIMARY KEY,
                task_id TEXT,
                sku TEXT,
                version INTEGER,
                body_json TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                session_id TEXT,
                turn INTEGER,
                text TEXT,
                sku TEXT,
                PRIMARY KEY (session_id, turn)
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def create_task(self, row: dict) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, session_id, idempotency_key, user_id, role, platform,
                    query, status, sku, clarify_question, candidates_json, draft_json,
                    draft_version, validation_json, retrieval_json, receipt_json,
                    cancelled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', '[]', '', 0, '', '', '', 0, ?, ?)
                """,
                (
                    row["task_id"],
                    row["session_id"],
                    row["idempotency_key"],
                    row["user_id"],
                    row["role"],
                    row["platform"],
                    row["query"],
                    "running",
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def find_task_by_key(self, idempotency_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT task_id FROM tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return None if row is None else row["task_id"]

    def update_task(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        columns = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [task_id]
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {columns} WHERE task_id = ?", values)
            self._conn.commit()

    def get_task_row(self, task_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return None if row is None else dict(row)

    def add_event(self, task_id: str, event: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (task_id, event, data_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, event, json.dumps(data, ensure_ascii=False), _now()),
            )
            self._conn.commit()

    def list_events(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, event, data_json, created_at FROM events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        events = []
        for row in rows:
            events.append(
                {
                    "id": row["id"],
                    "event": row["event"],
                    "data": json.loads(row["data_json"]),
                    "created_at": row["created_at"],
                }
            )
        return events

    def append_message(self, session_id: str, text: str, sku: str) -> None:
        with self._lock:
            current = self._conn.execute(
                "SELECT COALESCE(MAX(turn), 0) AS turn FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn = int(current["turn"]) + 1
            self._conn.execute(
                "INSERT INTO messages (session_id, turn, text, sku) VALUES (?, ?, ?, ?)",
                (session_id, turn, text, sku),
            )
            self._conn.commit()

    def history_texts(self, session_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT text FROM messages WHERE session_id = ? ORDER BY turn",
            (session_id,),
        ).fetchall()
        return [row["text"] for row in rows]

    def save_receipt_and_maybe_listing(
        self,
        key: str,
        task_id: str,
        receipt: dict,
        listing: dict | None,
    ) -> dict:
        """同一个键只落一次。已经有回执就直接把旧回执交回去。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                found = self._conn.execute(
                    "SELECT receipt_json FROM receipts WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if found is not None:
                    self._conn.execute("COMMIT")
                    return json.loads(found["receipt_json"])
                if listing is not None:
                    self._conn.execute(
                        """
                        INSERT INTO listings (listing_id, task_id, sku, version, body_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            listing["listing_id"],
                            task_id,
                            listing["sku"],
                            listing["version"],
                            json.dumps(listing["body"], ensure_ascii=False),
                            _now(),
                        ),
                    )
                stored = dict(receipt)
                self._conn.execute(
                    """
                    INSERT INTO receipts (idempotency_key, task_id, status, receipt_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        task_id,
                        stored["status"],
                        json.dumps(stored, ensure_ascii=False),
                        _now(),
                    ),
                )
                self._conn.execute("COMMIT")
                return stored
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_receipt(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT receipt_json FROM receipts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return None if row is None else json.loads(row["receipt_json"])

    def replace_receipt(self, key: str, receipt: dict, listing: dict | None) -> dict:
        """人工核对之后改回执。只有「结果不明」才允许改这一次。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._conn.execute(
                    "SELECT status FROM receipts WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if current is None or current["status"] != "unknown":
                    self._conn.execute("COMMIT")
                    raise RuntimeError("只有结果不明的写入才能人工核对")
                if listing is not None:
                    self._conn.execute(
                        """
                        INSERT INTO listings (listing_id, task_id, sku, version, body_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            listing["listing_id"],
                            listing["task_id"],
                            listing["sku"],
                            listing["version"],
                            json.dumps(listing["body"], ensure_ascii=False),
                            _now(),
                        ),
                    )
                self._conn.execute(
                    "UPDATE receipts SET status = ?, receipt_json = ? WHERE idempotency_key = ?",
                    (receipt["status"], json.dumps(receipt, ensure_ascii=False), key),
                )
                self._conn.execute("COMMIT")
                return receipt
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def listing_count(self, task_id: str | None = None) -> int:
        if task_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM listings WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["n"])
