"""持久化外部写入意图；未知结果只查询，绝不自动再次创建。"""

from dataclasses import asdict
from html import escape
import json
import sqlite3
import threading
import uuid

from app.listing_agent.ledger import _now
from app.listing_agent.shop_adapter import (
    ReconcileResult, ShopConflictError, ShopError, ShopUnknownError, WriteIntent,
)
from app.listing_agent.shopify_adapter import _payload_sha256
from app.listing_agent.task_lock import task_lock

TEST_STORE = "zhanggui-test.myshopify.com"
_TASK_STATUS = {"confirmed": "written", "unknown": "write_unknown",
                "sending": "write_unknown", "recorded": "write_unknown",
                "failed": "write_failed", "conflict": "write_conflict"}


class ShopWriter:
    def __init__(self, ledger, adapter, runtime):
        if adapter.store_domain != TEST_STORE:
            raise ShopError("当前版本只允许写入 zhanggui-test.myshopify.com 测试店")
        self.ledger, self.adapter, self.runtime = ledger, adapter, runtime
        self._lock = threading.RLock()
        self.db = sqlite3.connect(ledger.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS write_intents (
                operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                draft_version TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                store_domain TEXT NOT NULL, title TEXT NOT NULL, body_html TEXT NOT NULL,
                vendor TEXT NOT NULL, tags TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL, receipt_json TEXT NOT NULL DEFAULT '{}',
                reconcile_count INTEGER NOT NULL DEFAULT 0,
                last_reconciled_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(task_id, draft_version)
            );
            CREATE TABLE IF NOT EXISTS write_intent_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
                checked_at TEXT NOT NULL, result_json TEXT NOT NULL
            );
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, task_id, version):
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM write_intents WHERE task_id=? AND draft_version=?",
                (task_id, str(version)),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def intent(row):
        fields = {name: row[name] for name in WriteIntent.__dataclass_fields__}
        fields["tags"] = json.loads(fields["tags"])
        return WriteIntent(**fields)

    def reserve(self, task_id, version, draft):
        title, vendor = draft["title"], draft["brand"]
        body = "<ul>" + "".join("<li>" + escape(text) + "</li>" for text in draft["bullets"]) + "</ul>"
        tags = ["zhanggui-listing-test", "sku:" + draft["sku"]]
        digest = _payload_sha256(title, body, vendor, tags)
        intent = WriteIntent(uuid.uuid4().hex, task_id, str(version), self.adapter.store_domain,
                             title, body, vendor, tags, digest, f"{task_id}:{version}:write")
        values = asdict(intent)
        values["tags"] = json.dumps(tags, ensure_ascii=False)
        values.update(state="recorded", created_at=_now(), updated_at=_now())
        with self._lock, self.db:
            columns = ",".join(values)
            self.db.execute(
                f"INSERT OR IGNORE INTO write_intents ({columns}) VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )
        row = self.get(task_id, version)
        if row["payload_sha256"] != digest or row["store_domain"] != intent.store_domain:
            raise ShopConflictError("同一草稿版本的写入意图已绑定另一份内容或店铺，禁止重写")
        return row

    def _finish(self, row, state, receipt):
        """远端状态、任务和本地回执在同一 SQLite 事务中提交。"""
        receipt = {**receipt, "backend": "shopify", "operation_id": row["operation_id"],
                   "payload_sha256": row["payload_sha256"], "store_domain": row["store_domain"]}
        encoded = json.dumps(receipt, ensure_ascii=False)
        with self._lock, self.db:
            self.db.execute("UPDATE write_intents SET state=?, receipt_json=?, updated_at=? WHERE operation_id=?",
                            (state, encoded, _now(), row["operation_id"]))
            self.db.execute("""INSERT INTO receipts VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status, receipt_json=excluded.receipt_json""",
                (row["idempotency_key"], row["task_id"], receipt["status"], encoded, _now()))
            if state == "confirmed":
                task = self.db.execute("SELECT sku, draft_json FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
                self.db.execute("INSERT OR IGNORE INTO listings VALUES (?, ?, ?, ?, ?, ?)",
                                (receipt["listing_id"], row["task_id"], task["sku"], int(row["draft_version"]), task["draft_json"], _now()))
            self.db.execute("UPDATE tasks SET status=?, receipt_json=?, updated_at=? WHERE task_id=?",
                            (_TASK_STATUS[state], encoded, _now(), row["task_id"]))
        return receipt

    def write(self, task_id, version, draft):
        row = self.reserve(task_id, version, draft)
        if row["state"] in {"confirmed", "failed", "conflict"}:
            return json.loads(row["receipt_json"])
        with self._lock, self.db:
            claimed = self.db.execute("UPDATE write_intents SET state='sending', updated_at=? WHERE operation_id=? AND state='recorded'",
                                      (_now(), row["operation_id"])).rowcount
        if not claimed:
            return self.reconcile(row)
        # sending 已提交。此后任何异常或崩溃都按可能已发送处理。
        try:
            result = self.adapter.create_draft_product(intent=self.intent(row))
        except ShopConflictError as exc:
            return self._finish(row, "conflict", {"status": "conflict", "message": "远端商品与草稿目标冲突，需人工核对，禁止自动重建。", "raw": self._error(exc)})
        except ShopUnknownError:
            self._finish(row, "unknown", {"status": "unknown", "message": "创建结果未知，正在按操作标记核对。"})
            return self.reconcile(row)
        except ShopError as exc:
            return self._finish(row, "failed", {"status": "failed", "message": "请求前校验失败或 Shopify 明确拒绝；没有自动重试，请检查配置及业务错误。", "raw": self._error(exc)})
        except Exception:
            # 意外解析异常也不能回到可创建状态。
            return self._finish(row, "unknown", {"status": "unknown", "message": "创建调用异常，结果未知，请查询核对。"})
        if result.status != "DRAFT" or not result.shop_product_id:
            return self._finish(row, "conflict", {"status": "conflict", "message": "回执不满足 DRAFT 或缺少商品 ID，需人工核对。"})
        return self._finish(row, "confirmed", {
            "status": "written", "listing_id": result.shop_product_id,
            "shop_product_id": result.shop_product_id, "shop_status": result.status,
            "url": result.url, "raw": result.raw,
            "message": "已写入 Shopify 测试店草稿，并独立读回确认 DRAFT。",
        })

    def _error(self, exc):
        message = str(exc)
        for name in ("_legacy_token", "client_secret", "client_id", "_token"):
            value = getattr(self.adapter, name, None)
            if value:
                message = message.replace(value, "[REDACTED]")
        return {"error_type": type(exc).__name__, "detail": message}

    def reconcile(self, row):
        current = self.get(row["task_id"], row["draft_version"])
        if current["state"] == "confirmed":
            return json.loads(current["receipt_json"])
        if current["state"] == "failed":
            return json.loads(current["receipt_json"])
        try:
            result = self.adapter.reconcile(self.intent(current))
        except Exception:
            result = ReconcileResult("unknown", reason="核对查询失败，结果仍未知，禁止自动重建。")
        if result.status == "not_found":
            # 业务层从不把发送后的空搜索当作可重发证据。
            result = ReconcileResult("unknown", reason="已存在写入意图，未查到不足以证明未创建，禁止重建。")
        with self._lock, self.db:
            self.db.execute("INSERT INTO write_intent_checks (operation_id, checked_at, result_json) VALUES (?, ?, ?)",
                            (current["operation_id"], _now(), json.dumps(asdict(result), ensure_ascii=False)))
            self.db.execute("UPDATE write_intents SET reconcile_count=reconcile_count+1, last_reconciled_at=? WHERE operation_id=?",
                            (_now(), current["operation_id"]))
        product = result.product or {}
        if result.status == "found" and product.get("id") and product.get("status") == "DRAFT":
            product_id = str(product["id"]).rsplit("/", 1)[-1]
            return self._finish(current, "confirmed", {
                "status": "written", "listing_id": product_id, "shop_product_id": product_id,
                "shop_status": "DRAFT", "url": f"https://{current['store_domain']}/admin/products/{product_id}",
                "raw": {"reconcile": asdict(result), "readback_confirmed": True, "mutation_confirmed": False},
                "message": "已按操作标记和草稿内容找回 Shopify DRAFT 回执，没有再次创建。",
            })
        state = "conflict" if result.status == "conflict" else "unknown"
        return self._finish(current, state, {"status": state, "message": result.reason,
                                            "raw": {"reconcile": asdict(result)}})

    def recover(self):
        with self._lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM write_intents WHERE state IN ('recorded','sending','unknown')")]
        for row in rows:
            with task_lock(self.runtime, row["task_id"]):
                self.reconcile(row)
