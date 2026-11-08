"""上线安全边界、在线统计、完整备份恢复；所有店铺请求均为离线模拟。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app.listing_agent.auth import AuthStore, load_accounts_from_env
from app.listing_agent.backup import backup, restore
from app.listing_agent.dashboard import summarize
from app.listing_agent.deployment import runtime_lease, validate_public_config
from app.listing_agent.service import ListingService
from test.listing.test_shopify_integration import PersistentFakeShop, QUERY


class DeploymentTest(unittest.TestCase):
    def test_public_config_rejects_unsafe_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "LISTING_DEPLOYMENT": "public", "LISTING_SHOP_BACKEND": "shopify",
                "SHOPIFY_STORE_DOMAIN": "zhanggui-test.myshopify.com", "SHOPIFY_ADMIN_TOKEN": "fake",
                "LISTING_DRAFT_MODE": "offline", "LISTING_CHECKPOINT": "sqlite",
                "PYTHON_DOTENV_DISABLED": "1", "LISTING_ACCOUNTS": "owner:" + "x" * 25 + ":editor:org",
                "LISTING_RUNTIME_DIR": str(Path(directory) / "runtime"), "LISTING_VOLUME_DIR": directory,
            }
            with patch.dict(os.environ, env, clear=True), patch("os.path.ismount", return_value=True):
                validate_public_config()
                for key, value in [("SHOPIFY_STORE_DOMAIN", "other.myshopify.com"),
                                   ("LISTING_ACCOUNTS", "小明:demo-pass:editor:org"),
                                   ("LISTING_ACCOUNTS", "owner:replace-with-at-least-20-random-characters:editor:org"),
                                   ("LISTING_TEST_MODE", "1"), ("LISTING_SHOP_BACKEND", "local"),
                                   ("LISTING_DRAFT_MODE", "llm"), ("SHOPIFY_ADMIN_TOKEN", "")]:
                    with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                        with self.assertRaises(RuntimeError):
                            validate_public_config()
                with patch("os.path.ismount", return_value=False):
                    with self.assertRaises(RuntimeError):
                        validate_public_config()

    def test_account_changes_revoke_sessions_and_public_removes_old_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(Path(directory) / "auth.sqlite3")
            try:
                store.register("old", "old-password", "editor", "org")
                old_token = store.login("old", "old-password")
                store.register("owner", "old-password", "editor", "org")
                token = store.login("owner", "old-password")
                with patch.dict(os.environ, {"LISTING_DEPLOYMENT": "public", "LISTING_ACCOUNTS": "owner:new-long-password-for-owner:editor:org"}):
                    load_accounts_from_env(store)
                self.assertIsNone(store.resolve(token))
                self.assertIsNone(store.resolve(old_token))
                self.assertIsNone(store.login("old", "old-password"))
                token = store.login("owner", "new-long-password-for-owner")
                with patch.dict(os.environ, {"LISTING_DEPLOYMENT": "public", "LISTING_ACCOUNTS": "owner:new-long-password-for-owner:editor:org"}):
                    load_accounts_from_env(store)
                self.assertIsNotNone(store.resolve(token))
                store._conn.execute("UPDATE sessions SET expires_at = 0")
                store._conn.commit()
                self.assertIsNone(store.resolve(token))
            finally:
                store.close()

    def test_backup_restore_shop_receipt_checkpoints_auth_and_repeated_confirm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            shop = PersistentFakeShop(root / "remote.sqlite")
            service = ListingService(runtime, shop_adapter=shop)
            task = service.start(QUERY, user_id="owner", role="editor", draft_mode="offline")
            written = service.confirm(task["task_id"], "owner", "editor", task["draft_version"])
            service.close()
            auth = AuthStore(runtime / "auth.sqlite3")
            auth.register("owner", "private-long-password", "editor", "org")
            token = auth.login("owner", "private-long-password")
            auth.close()
            before = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in runtime.rglob("*") if p.is_file()}
            archive = root / "backup.zip"
            with runtime_lease(runtime):
                with self.assertRaises(RuntimeError):
                    backup(runtime, archive)
            backup(runtime, archive)
            # 仅删除本测试创建的临时运行目录，模拟丢失；远端模拟店铺保留。
            self.assertEqual(runtime.parent, root)
            shutil.rmtree(runtime)
            restore(archive, runtime)
            after = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in runtime.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            auth = AuthStore(runtime / "auth.sqlite3")
            self.assertIsNotNone(auth.resolve(token))
            auth.close()
            service = ListingService(runtime, shop_adapter=shop)
            try:
                snapshot = service.snapshot(task["task_id"])
                self.assertEqual(snapshot["receipt"], written["receipt"])
                self.assertTrue(snapshot["events"])
                service.confirm(task["task_id"], "owner", "editor", task["draft_version"])
                self.assertEqual(len(shop.products()), 1)
                self.assertIsNotNone(service.checkpoints.saver.get_tuple({"configurable": {"thread_id": task["task_id"]}}))
            finally:
                service.close()
            with self.assertRaises(ValueError):
                restore(archive, runtime)

    def test_restore_rejects_corruption_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, digest) in enumerate([("../outside.txt", hashlib.sha256(b"data").hexdigest()),
                                                     ("file.txt", "wrong")]):
                archive = root / f"bad-{index}.zip"
                with zipfile.ZipFile(archive, "w") as handle:
                    handle.writestr(name, b"data")
                    handle.writestr("backup_manifest.json", json.dumps({"format": 1, "files": {name: digest}}))
                with self.assertRaises(ValueError):
                    restore(archive, root / f"target-{index}")
            self.assertFalse((root / "outside.txt").exists())

    def test_online_dashboard_deduplicates_status_and_sums_all_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                {"trace_id": "a", "mode": "knowledge", "cost": 0, "attempts": [
                    {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.1},
                    {"prompt_tokens": None, "completion_tokens": None, "cost": None}]},
                {"trace_id": "b", "mode": "llm", "attempts": [
                    {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.2},
                    {"prompt_tokens": 9, "completion_tokens": 4, "cost": 0.3}]},
                {"trace_id": "c", "mode": "offline"},
            ]
            (root / "draft_telemetry.jsonl").write_text("\n".join(json.dumps(row) for row in rows * 3), encoding="utf-8")
            (root / "llm_draft_telemetry.jsonl").write_text(json.dumps({"trace_id": "offline-eval", "cost": 999}), encoding="utf-8")
            data = summarize(root, [{"task_id": key, "status": "written"} for key in ("a", "b", "c")])
            self.assertEqual(data["task_count"], 3)
            a, b, c = data["tasks"]
            self.assertIsNone(a["cost"])
            self.assertIsNone(a["prompt_tokens"])
            self.assertEqual(a["provider_attempts"], 2)
            self.assertEqual(b["cost"], 0.5)
            self.assertEqual(b["prompt_tokens"], 16)
            self.assertEqual(c["cost"], 0)
            self.assertIsNone(data["totals"]["cost"])


class BrowserAuthTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.listing_agent import api
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LISTING_RUNTIME_DIR": self.tmp.name,
                             "PYTHON_DOTENV_DISABLED": "1", "LISTING_SHOP_BACKEND": "local"})
        self.env.start()
        self.api = api
        if api._service:
            api._service.close()
        if api._auth:
            api._auth.close()
        api._service = api._auth = None
        api.get_auth().register("owner", "test-owner-password", "editor", "org")
        self.client = TestClient(api.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        if self.api._service:
            self.api._service.close()
        if self.api._auth:
            self.api._auth.close()
        self.api._service = self.api._auth = None
        self.env.stop()
        self.tmp.cleanup()

    def test_cookie_login_page_csrf_logout_and_dashboard(self):
        self.assertEqual(self.client.get("/listing.html", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/listing/dashboard").status_code, 401)
        with patch.dict(os.environ, {"LISTING_DEPLOYMENT": "public"}):
            login = self.client.post("/api/listing/sessions", json={"user_id": "owner", "password": "test-owner-password"})
        cookie = login.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(self.client.get("/listing.html").status_code, 200)
        denied = self.client.post("/api/listing/tasks", json={"query": QUERY}, headers={"Origin": "https://evil.example"})
        self.assertEqual(denied.status_code, 403)
        created = self.client.post("/api/listing/tasks", json={"query": QUERY}, headers={"Origin": "https://testserver"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.client.get("/api/listing/dashboard").json()["task_count"], 1)
        with patch.dict(os.environ, {"LISTING_DEPLOYMENT": "public"}):
            denied = self.client.post("/api/listing/tasks", json={"query": QUERY, "draft_mode": "llm"}, headers={"Origin": "https://testserver"})
            self.assertEqual(denied.status_code, 403)
        logged_out = self.client.post("/api/listing/logout", headers={"Origin": "https://testserver"})
        self.assertEqual(logged_out.status_code, 200)
        self.assertEqual(self.client.get("/api/listing/me").status_code, 401)
