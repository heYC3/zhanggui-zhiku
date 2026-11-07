"""真实业务链路 + 持久化模拟店铺。所有测试均不联网。"""

from dataclasses import replace
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.listing_agent.paths import PACKAGE_ROOT
from app.listing_agent.rules import Actor, validate_listing_draft
from app.listing_agent.service import ListingError, ListingService
from app.listing_agent.shop_adapter import ShopError, ShopUnknownError
from app.listing_agent.shopify_adapter import ShopifyAdapter, _op_tag

QUERY = "掌柜示例耳机 X1 的蓝牙版本，写成要点"


@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            yield db
    finally:
        db.close()


class PersistentFakeShop(ShopifyAdapter):
    def __init__(self, path):
        super().__init__(store_domain="zhanggui-test.myshopify.com", legacy_admin_token="shpat_fake")
        self.path = Path(path)
        self.lost_response = self.query_down = self.hide_search = self.reject = self.preflight_error = False
        self.active = self.malformed = self.crash_after_create = False
        self.ledger_path = None
        with database(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, body_json TEXT)")

    def products(self):
        with database(self.path) as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body_json FROM products")]

    def create_draft_product(self, *, intent):
        if self.preflight_error:
            raise ShopError("本地请求前校验失败")
        if self.ledger_path:
            with database(self.ledger_path) as db:
                row = db.execute("SELECT state FROM write_intents WHERE operation_id=?", (intent.operation_id,)).fetchone()
                assert row and row[0] == "sending", "发请求前意图必须已持久化"
        return super().create_draft_product(intent=intent)

    def _graphql(self, query, variables=None):
        if "productCreate" in query:
            if self.reject:
                return {"productCreate": {"product": None, "userErrors": [{"field": ["title"], "message": "rejected"}]}}
            product = dict(variables["product"])
            with database(self.path) as db:
                cursor = db.execute("INSERT INTO products(body_json) VALUES ('{}')")
                product["id"] = f"gid://shopify/Product/{cursor.lastrowid}"
                if self.active:
                    product["status"] = "ACTIVE"
                db.execute("UPDATE products SET body_json=? WHERE id=?", (json.dumps(product, ensure_ascii=False), cursor.lastrowid))
            if self.crash_after_create:
                os._exit(87)
            if self.lost_response:
                raise ShopUnknownError("远端已提交但响应丢失")
            if self.malformed:
                return {"productCreate": None}
            return {"productCreate": {"product": product, "userErrors": []}}
        if self.query_down:
            raise ShopUnknownError("核对超时")
        products = self.products()
        if "productsByTag" in query:
            tag = variables["query"].removeprefix("tag:")
            matches = [] if self.hide_search else [p for p in products if tag in p["tags"]]
            return {"products": {"edges": [{"node": p} for p in matches]}}
        if "getProduct" in query:
            return {"product": next((p for p in products if p["id"] == variables["id"]), None)}
        raise AssertionError("未预期查询")


class ShopifyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.shop = PersistentFakeShop(self.root / "remote.sqlite")
        self.service = ListingService(self.root / "runtime", shop_adapter=self.shop)
        self.shop.ledger_path = self.service.ledger.path

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def start(self):
        return self.service.start(QUERY, user_id="owner", role="editor", draft_mode="offline")

    def confirm(self, task):
        return self.service.confirm(task["task_id"], "owner", "editor", task["draft_version"])

    def restart(self):
        self.service.close()
        self.service = ListingService(self.root / "runtime", shop_adapter=self.shop)

    def test_confirm_once_duplicate_and_graph_replay(self):
        task = self.start()
        result = self.confirm(task)
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["receipt"]["shop_status"], "DRAFT")
        self.assertTrue(result["receipt"]["raw"]["mutation_confirmed"])
        self.assertTrue(result["receipt"]["raw"]["readback_confirmed"])
        self.confirm(task)
        self.service.shop_writer.write(task["task_id"], task["draft_version"], task["draft"])
        self.assertEqual(len(self.shop.products()), 1)
        self.assertEqual(self.service.ledger.listing_count(), 1)

    def test_response_lost_recovers_without_create(self):
        self.shop.lost_response = True
        task = self.start()
        result = self.confirm(task)
        self.assertEqual(result["status"], "written")
        self.assertFalse(result["receipt"]["raw"]["mutation_confirmed"])
        self.confirm(task)
        self.assertEqual(len(self.shop.products()), 1)

    def test_query_timeout_and_restart_do_not_recreate(self):
        self.shop.lost_response = self.shop.query_down = True
        task = self.start()
        self.assertEqual(self.confirm(task)["status"], "write_unknown")
        self.confirm(task)
        self.restart()
        self.assertEqual(self.service.snapshot(task["task_id"])["status"], "write_unknown")
        row = self.service.shop_writer.get(task["task_id"], 1)
        self.assertGreaterEqual(row["reconcile_count"], 3)
        self.assertIsNotNone(row["last_reconciled_at"])
        self.shop.query_down = False
        fixed = self.service.reconcile(task["task_id"], "owner", "editor", found=False)
        self.assertEqual(fixed["status"], "written")
        self.assertEqual(len(self.shop.products()), 1)

    def test_empty_search_remains_unknown(self):
        self.shop.lost_response = self.shop.hide_search = True
        task = self.start()
        self.assertEqual(self.confirm(task)["status"], "write_unknown")
        self.confirm(task)
        self.restart()
        self.assertEqual(len(self.shop.products()), 1)
        self.assertEqual(self.service.snapshot(task["task_id"])["status"], "write_unknown")

    def test_request_before_failure_and_business_rejection(self):
        for flag in ("preflight_error", "reject"):
            setattr(self.shop, flag, True)
            task = self.start()
            self.assertEqual(self.confirm(task)["status"], "write_failed")
            self.confirm(task)
            setattr(self.shop, flag, False)
        self.assertEqual(len(self.shop.products()), 0)

    def test_non_draft_is_conflict_and_preserves_evidence(self):
        self.shop.active = True
        task = self.start()
        result = self.confirm(task)
        self.assertEqual(result["status"], "write_conflict")
        self.assertIn("gid://shopify/Product/1", result["receipt"]["raw"]["detail"])
        self.confirm(task)
        self.assertEqual(len(self.shop.products()), 1)
        self.assertEqual(self.service.ledger.listing_count(), 0)

    def test_parse_failure_can_reconcile_existing_product(self):
        self.shop.malformed = True
        task = self.start()
        self.assertEqual(self.confirm(task)["status"], "written")
        self.assertEqual(len(self.shop.products()), 1)

    def test_same_title_other_product_not_a_receipt(self):
        task = self.start()
        self.shop.hide_search = self.shop.lost_response = True
        self.assertEqual(self.confirm(task)["status"], "write_unknown")
        product = self.shop.products()[0]
        product["tags"] = ["unrelated"]
        with database(self.shop.path) as db:
            db.execute("UPDATE products SET body_json=?", (json.dumps(product),))
        self.shop.hide_search = False
        self.assertEqual(self.confirm(task)["status"], "write_unknown")
        self.assertEqual(len(self.shop.products()), 1)

    def test_concurrent_clicks(self):
        task = self.start()
        errors = []
        def run():
            try:
                self.confirm(task)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.shop.products()), 1)

    def test_owner_and_version_gates_before_any_write(self):
        task = self.start()
        for user, role, version in [("stranger", "editor", 1), ("owner", "viewer", 1), ("owner", "editor", 99)]:
            with self.assertRaises(ListingError):
                self.service.confirm(task["task_id"], user, role, version)
        self.assertEqual(len(self.shop.products()), 0)
        self.assertIsNone(self.service.shop_writer.get(task["task_id"], 1))

    def test_api_reconcile_uses_remote_result_instead_of_boolean(self):
        from fastapi.testclient import TestClient
        from app.listing_agent import api
        from app.listing_agent.auth import AuthStore
        import secrets
        auth = AuthStore(self.root / "test-auth.sqlite3")
        password = secrets.token_urlsafe(16)
        auth.register("owner", password, "editor", "test")
        try:
            with patch.object(api, "_service", self.service), patch.object(api, "_auth", auth), TestClient(api.app) as client:
                self.assertEqual(client.post("/api/listing/tasks", json={"query": QUERY}).status_code, 401)
                login = client.post("/api/listing/sessions", json={"user_id": "owner", "password": password})
                client.headers["Authorization"] = "Bearer " + login.json()["token"]
                created = client.post("/api/listing/tasks", json={"query": QUERY, "draft_mode": "offline"}).json()
                self.shop.lost_response = self.shop.query_down = True
                endpoint = "/api/listing/tasks/" + created["task_id"]
                unknown = client.post(endpoint + "/confirm", json={"draft_version": 1})
                self.assertEqual(unknown.json()["status"], "write_unknown")
                self.assertEqual(client.post(endpoint + "/reconcile", json={"found": False}).json()["status"], "write_unknown")
                self.shop.query_down = False
                found = client.post(endpoint + "/reconcile", json={})
                self.assertEqual(found.status_code, 200)
                self.assertEqual(found.json()["status"], "written")
                self.assertEqual(len(self.shop.products()), 1)
        finally:
            auth.close()

    def test_title_cannot_add_unsupported_words_without_numbers(self):
        task = self.start()
        bad = dict(task["draft"], title="掌柜示例X1 完全防水")
        self.assertFalse(validate_listing_draft(bad, "amazon", Actor("owner", "editor"))["ok"])

    def test_remote_runtime_cannot_silently_switch_to_local(self):
        task = self.start()
        self.confirm(task)
        with self.assertRaises(ListingError):
            ListingService(self.root / "runtime", shop_backend="local")
        self.assertEqual(len(self.shop.products()), 1)

    def test_env_loaded_before_runtime_and_login_configuration(self):
        from app.listing_agent import api
        expected = self.root / "configured-runtime"
        config = self.root / "acceptance.env"
        config.write_text("LISTING_RUNTIME_DIR=" + str(expected) + "\nLISTING_SHOP_BACKEND=local\n", encoding="utf-8")
        with patch.dict(os.environ, {"SHOPIFY_ENV_FILE": str(config), "PYTHON_DOTENV_DISABLED": "0"}):
            os.environ.pop("LISTING_RUNTIME_DIR", None)
            os.environ.pop("LISTING_SHOP_BACKEND", None)
            configured = ListingService()
            try:
                self.assertEqual(configured.runtime, expected)
                self.assertEqual(configured.shop_backend, "local")
            finally:
                configured.close()
            with patch.object(api, "_auth", None), patch("app.listing_agent.api.load_accounts_from_env") as accounts:
                auth = api.get_auth()
                try:
                    self.assertTrue((expected / "auth.sqlite3").exists())
                    accounts.assert_called_once_with(auth)
                finally:
                    auth.close()

    def worker(self, task, crash=False, marker=None):
        code = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_shopify_integration import PersistentFakeShop
from app.listing_agent.service import ListingService
root, task_id = Path(sys.argv[2]), sys.argv[3]
shop = PersistentFakeShop(root / 'remote.sqlite')
shop.crash_after_create = sys.argv[4] == '1'
service = ListingService(root / 'runtime', shop_adapter=shop)
if sys.argv[5]:
    marker = Path(sys.argv[5])
    while not marker.exists(): time.sleep(0.02)
try:
    result = service.confirm(task_id, 'owner', 'editor', 1)
    print(result['status'])
finally:
    service.close()
"""
        return subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).parent), str(self.root), task["task_id"], "1" if crash else "0", str(marker or "")],
                                cwd=PACKAGE_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_process_exits_after_remote_commit_before_local_commit(self):
        task = self.start()
        process = self.worker(task, crash=True)
        output, errors = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 87, errors)
        self.assertEqual(len(self.shop.products()), 1)
        self.assertEqual(self.service.shop_writer.get(task["task_id"], 1)["state"], "sending")
        self.restart()
        self.assertEqual(self.service.snapshot(task["task_id"])["status"], "written")
        self.confirm(task)
        self.assertEqual(len(self.shop.products()), 1)

    def test_two_processes_confirm_create_only_once(self):
        task = self.start()
        marker = self.root / "go"
        processes = [self.worker(task, marker=marker) for _ in range(2)]
        marker.write_text("go", encoding="utf-8")
        for process in processes:
            output, errors = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(len(self.shop.products()), 1)
        self.assertEqual(self.service.ledger.listing_count(), 1)


if __name__ == "__main__":
    unittest.main()
