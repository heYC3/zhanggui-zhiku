"""LangGraph 检查点。

默认用官方 SqliteSaver，每一步写入 SQLite。进程被强制结束后，确认点还在。
LISTING_CHECKPOINT=file 时仍用内存检查点加文件快照，只适合单实例演示。
那种快照要等本次调用结束才落盘，保存前被杀掉就续不上。
"""

from __future__ import annotations

import os
import pickle
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver


def _namespace() -> dict:
    return defaultdict(dict)


def _to_plain(value):
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    return value


class CheckpointStore:
    def __init__(self, path: Path, kind: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.kind = (kind or os.getenv("LISTING_CHECKPOINT", "sqlite")).strip().lower() or "sqlite"
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self.kind == "file":
            self.saver = InMemorySaver()
            self.load()
        else:
            self.kind = "sqlite"
            database = self.path.with_suffix(".sqlite")
            self._conn = sqlite3.connect(str(database), check_same_thread=False, timeout=5)
            self._conn.execute("PRAGMA busy_timeout=5000")
            self.saver = SqliteSaver(self._conn)
            for attempt in range(20):
                try:
                    self.saver.setup()
                    break
                except sqlite3.OperationalError:
                    if attempt == 7:
                        raise
                    time.sleep(0.25)
            self._arm_sqlite_crash()

    def _arm_sqlite_crash(self) -> None:
        """SQLite 的落盘点是 put。保存前退出，等于这一步检查点没有写上。"""
        if os.getenv("LISTING_CRASH_AT", "").strip() != "before_checkpoint_save":
            return
        original = self.saver.put

        def crashing_put(*args, **kwargs):
            os._exit(86)

        self.saver.put = crashing_put
        self._original_put = original

    def load(self) -> None:
        if self.kind != "file" or not self.path.exists():
            return
        with self.path.open("rb") as handle:
            data = pickle.load(handle)
        storage = defaultdict(_namespace)
        for thread_id, namespaces in data["storage"].items():
            for name, checkpoints in namespaces.items():
                storage[thread_id][name].update(checkpoints)
        writes = defaultdict(dict)
        for key, bucket in data["writes"].items():
            writes[key].update(bucket)
        self.saver.storage = storage
        self.saver.writes = writes
        self.saver.blobs = dict(data["blobs"])

    def save(self) -> None:
        if self.kind == "file" and os.getenv("LISTING_CRASH_AT", "").strip() == "before_checkpoint_save":
            os._exit(86)
        if self.kind != "file":
            if self._conn is not None:
                self._conn.commit()
            return
        payload = {
            "storage": _to_plain(self.saver.storage),
            "writes": _to_plain(self.saver.writes),
            "blobs": _to_plain(self.saver.blobs),
        }
        temporary = self.path.with_suffix(".pkl.tmp")
        with self._lock:
            with temporary.open("wb") as handle:
                pickle.dump(payload, handle)
            temporary.replace(self.path)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
