"""Listing 闭环回归。不连 Milvus，也不调用付费模型。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.listing_agent.catalog import resolve_product
from app.listing_agent.eval import assert_report, run_eval
from app.listing_agent.mcp_server import call_tool
from app.listing_agent.retrieve import search_evidence
from app.listing_agent.rules import Actor, validate_listing_draft
from app.listing_agent.service import ListingError, ListingService


class ListingLoopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ListingService(Path(self._tmp.name))

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _start(self, query: str, **kwargs):
        body = {"user_id": "小明", "role": "editor"}
        body.update(kwargs)
        return self.service.start(query, **body)

    def test_paraphrase_then_confirm_and_restart(self):
        first = self._start("掌柜示例耳机 X1 单次充电能听多久，写成亚马逊要点")
        self.assertEqual(first["status"], "awaiting_confirmation")
        self.assertIn("30", first["draft"]["title"])
        self.assertNotIn("防水", first["draft"]["title"])
        self.assertTrue(first["validation"]["ok"])
        self.assertTrue((Path(self._tmp.name) / "traces.jsonl").exists())

        task_id = first["task_id"]
        version = first["draft_version"]
        self.service.close()
        restarted = ListingService(Path(self._tmp.name))
        try:
            written = restarted.confirm(task_id, "小明", "editor", version)
            self.assertEqual(written["status"], "written")
            self.assertEqual(restarted.ledger.listing_count(task_id), 1)
            with self.assertRaises(ListingError):
                restarted.confirm(task_id, "小明", "editor", version)
            self.assertEqual(restarted.ledger.listing_count(task_id), 1)
        finally:
            restarted.close()

    def test_ambiguous_missing_and_pronoun(self):
        fuzzy = self._start("掌柜示例耳机能听多久")
        self.assertEqual(fuzzy["status"], "ambiguous")
        self.assertIsNone(fuzzy["draft"])

        missing = self._start("给星河无人机 Mini 写商品标题")
        self.assertEqual(missing["status"], "insufficient")
        self.assertIsNone(missing["draft"])

        self.service.ledger.append_message("talk-1", "我在看掌柜示例耳机 X1 Pro", "")
        follow = self._start("把它写成亚马逊五点", session_id="talk-1")
        self.assertEqual(follow["sku"], "zg-ear-x1-pro")
        self.assertIn("降噪", "\n".join(follow["draft"]["bullets"]))

    def test_rerank_down_still_drafts(self):
        result = self._start(
            "掌柜示例耳机 X1 的蓝牙版本，写成要点",
            rerank_mode="down",
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertTrue(result["retrieval"]["rerank_degraded"])
        self.assertIn("5.3", "\n".join(result["draft"]["bullets"]))

    def test_viewer_version_cancel_and_duplicate(self):
        created = self._start(
            "掌柜示例耳机 X1 的蓝牙版本，写成要点",
            idempotency_key="same-key",
        )
        duplicate = self._start(
            "掌柜示例耳机 X1 的蓝牙版本，写成要点",
            idempotency_key="same-key",
        )
        self.assertEqual(created["task_id"], duplicate["task_id"])
        with self.assertRaises(ListingError) as conflict:
            self._start("另一句完全不同的请求", idempotency_key="same-key")
        self.assertEqual(conflict.exception.status_code, 409)

        with self.assertRaises(ListingError):
            self.service.confirm(created["task_id"], "小明", "viewer", created["draft_version"])
        with self.assertRaises(ListingError):
            self.service.confirm(created["task_id"], "别人", "editor", created["draft_version"])
        with self.assertRaises(ListingError):
            self.service.confirm(created["task_id"], "小明", "editor", 99)

        cancelled = self.service.cancel(created["task_id"], "小明", "editor")
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(ListingError):
            self.service.confirm(created["task_id"], "小明", "editor", created["draft_version"])
        self.assertEqual(self.service.ledger.listing_count(created["task_id"]), 0)

    def test_unknown_write_then_reconcile_once(self):
        created = self._start(
            "掌柜示例耳机 X1 的蓝牙版本，写成要点",
            inject_write_unknown=True,
        )
        unknown = self.service.confirm(created["task_id"], "小明", "editor", created["draft_version"])
        self.assertEqual(unknown["status"], "write_unknown")
        self.assertEqual(self.service.ledger.listing_count(created["task_id"]), 0)

        present = self.service.reconcile(created["task_id"], "小明", "editor", found=True)
        self.assertEqual(present["status"], "written_checked")
        self.assertEqual(self.service.ledger.listing_count(created["task_id"]), 0)

        other = self._start(
            "掌柜示例移动电源 P20 的容量，写成要点",
            inject_write_unknown=True,
        )
        self.service.confirm(other["task_id"], "小明", "editor", other["draft_version"])
        filled = self.service.reconcile(other["task_id"], "小明", "editor", found=False)
        self.assertEqual(filled["status"], "written")
        self.assertEqual(self.service.ledger.listing_count(other["task_id"]), 1)
        with self.assertRaises(ListingError):
            self.service.reconcile(other["task_id"], "小明", "editor", found=False)

    def test_gap_and_forbidden_word(self):
        created = self._start("掌柜示例耳机 X1 写上防水，做成亚马逊要点")
        self.assertTrue(created["draft"]["gaps"])
        self.assertNotIn("防水", created["draft"]["title"])
        bad = dict(created["draft"])
        bad["title"] = "掌柜示例X1 最强耳机"
        validation = validate_listing_draft(bad, "amazon", Actor("小明", "editor"))
        self.assertFalse(validation["ok"])

    def test_x1_pro_is_not_x1(self):
        found = resolve_product("掌柜示例耳机 X1 Pro 有没有主动降噪")
        self.assertEqual(found["sku"], "zg-ear-x1-pro")
        evidence = search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1", strategy="sparse")
        self.assertEqual(evidence["evidence"][0]["id"], "zg-ear-x1-link")
        from app.evidence.store import retrieve_product_evidence

        shared = retrieve_product_evidence(
            "掌柜示例耳机 X1 的蓝牙版本",
            product_id="掌柜示例耳机 X1",
            strategy="sparse",
        )
        self.assertEqual(shared["evidence"][0]["chunk_id"], evidence["evidence"][0]["id"])
        self.assertIn("蓝牙版本是 5.3", shared["evidence"][0]["text"])
        self.assertIn("zg-ear-x1-link", shared["evidence"][0]["source"])

    def test_eval_report_holds(self):
        assert_report(run_eval())

    def test_mcp_rejects_stranger_and_checks_draft(self):
        denied = call_tool(
            "lookup_product_evidence",
            {"query": "蓝牙", "actor_id": "", "role": "guest"},
        )
        self.assertTrue(denied["isError"])
        created = self._start("掌柜示例耳机 X1 的蓝牙版本，写成要点")
        checked = call_tool(
            "validate_listing_draft",
            {
                "draft": created["draft"],
                "platform": "amazon",
                "actor_id": "小明",
                "role": "viewer",
            },
        )
        self.assertFalse(checked["isError"])
        payload = json.loads(checked["content"][0]["text"])
        self.assertTrue(payload["ok"])

    def test_official_mcp_client_init_list_call(self):
        from app.listing_agent.mcp_probe import run_official_client

        record_path = Path(self._tmp.name) / "mcp_client_record.json"
        record = run_official_client(record_path)
        steps = {item["step"]: item for item in record["steps"]}
        self.assertEqual(steps["initialize"]["server_name"], "zhanggui-listing")
        self.assertEqual(
            steps["list_tools"]["tools"],
            ["lookup_product_evidence", "validate_listing_draft"],
        )
        self.assertFalse(steps["call"]["is_error"])
        self.assertIn("5.3", steps["call"]["text"])
        self.assertTrue(steps["bad_arguments"]["is_error"])
        self.assertIsNotNone(steps["process_exit"]["returncode"])
        self.assertTrue(record_path.exists())


class ListingApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LISTING_RUNTIME_DIR"] = self._tmp.name
        os.environ.pop("LISTING_TEST_MODE", None)
        from fastapi.testclient import TestClient

        import app.listing_agent.api as api
        from app.listing_agent.auth import AuthStore
        from app.listing_agent.paths import runtime_dir

        api._service = None
        if api._auth is not None:
            api._auth.close()
            api._auth = None
        store = AuthStore(runtime_dir() / "auth.sqlite3")
        store.register("小明", "pw-editor", "editor", "org-a")
        store.register("查看", "pw-viewer", "viewer", "org-a")
        store.register("别人", "pw-other", "editor", "org-b")
        store.close()
        self.api = api
        self.client = TestClient(api.app)

    def tearDown(self):
        if self.api._service is not None:
            self.api._service.close()
            self.api._service = None
        if self.api._auth is not None:
            self.api._auth.close()
            self.api._auth = None
        os.environ.pop("LISTING_TEST_MODE", None)
        self._tmp.cleanup()

    def _token(self, user_id: str, password: str) -> str:
        response = self.client.post(
            "/api/listing/sessions",
            json={"user_id": user_id, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_page_and_confirm(self):
        page = self.client.get("/listing.html")
        self.assertEqual(page.status_code, 200)
        self.assertIn("登录", page.text)
        self.assertIn("原句模板，不调用模型", page.text)
        self.assertIn("真实模型，仍要人工确认", page.text)
        token = self._token("小明", "pw-editor")
        created = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(token),
            json={
                "query": "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                "user_id": "别人",
                "role": "viewer",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["user_id"], "小明")
        self.assertEqual(body["role"], "editor")
        self.assertEqual(body["status"], "awaiting_confirmation")
        confirmed = self.client.post(
            f"/api/listing/tasks/{body['task_id']}/confirm",
            headers=self._headers(token),
            json={"user_id": "别人", "role": "editor", "draft_version": body["draft_version"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "written")

    def test_anonymous_and_cross_user_and_forged_role(self):
        missing = self.client.get("/api/listing/tasks/no-such")
        self.assertEqual(missing.status_code, 401)
        editor = self._token("小明", "pw-editor")
        viewer = self._token("查看", "pw-viewer")
        other = self._token("别人", "pw-other")
        created = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(editor),
            json={"query": "掌柜示例耳机 X1 的蓝牙版本，写成要点", "session_id": "talk-owner"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        task_id = created.json()["task_id"]
        stolen = self.client.get(f"/api/listing/tasks/{task_id}", headers=self._headers(other))
        self.assertEqual(stolen.status_code, 404)
        stream = self.client.get(f"/api/listing/stream/{task_id}", headers=self._headers(other))
        self.assertEqual(stream.status_code, 404)
        forged = self.client.post(
            f"/api/listing/tasks/{task_id}/confirm",
            headers=self._headers(viewer),
            json={"role": "editor", "draft_version": created.json()["draft_version"]},
        )
        self.assertEqual(forged.status_code, 404)
        own_viewer = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(viewer),
            json={"query": "掌柜示例耳机 X1 的蓝牙版本，写成要点"},
        )
        self.assertEqual(own_viewer.status_code, 200, own_viewer.text)
        denied = self.client.post(
            f"/api/listing/tasks/{own_viewer.json()['task_id']}/confirm",
            headers=self._headers(viewer),
            json={"role": "editor", "draft_version": own_viewer.json()["draft_version"]},
        )
        self.assertEqual(denied.status_code, 403)
        leaked = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(other),
            json={"query": "它的蓝牙版本，写成要点", "session_id": "talk-owner"},
        )
        self.assertEqual(leaked.status_code, 404)

    def test_idempotency_replay_conflict_and_concurrency(self):
        import threading

        token = self._token("小明", "pw-editor")
        payload = {
            "query": "掌柜示例耳机 X1 的蓝牙版本，写成要点",
            "idempotency_key": "same-http-key",
        }
        first = self.client.post("/api/listing/tasks", headers=self._headers(token), json=payload)
        second = self.client.post("/api/listing/tasks", headers=self._headers(token), json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["task_id"], second.json()["task_id"])
        conflict = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(token),
            json={"query": "另一份完全不同的内容", "idempotency_key": "same-http-key"},
        )
        self.assertEqual(conflict.status_code, 409)
        blocked = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(token),
            json={"query": "掌柜示例耳机 X1 的蓝牙版本，写成要点", "inject_write_unknown": True},
        )
        self.assertEqual(blocked.status_code, 403)

        os.environ["LISTING_TEST_MODE"] = "1"
        allowed = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(token),
            json={
                "query": "掌柜示例移动电源 P20 的容量，写成要点",
                "inject_write_unknown": True,
            },
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

        barrier = threading.Barrier(2)
        results = []

        def submit():
            barrier.wait()
            response = self.client.post(
                "/api/listing/tasks",
                headers=self._headers(token),
                json={
                    "query": "掌柜示例耳机 X1 单耳重量，写成要点",
                    "idempotency_key": "parallel-key",
                },
            )
            results.append((response.status_code, response.json().get("task_id")))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([item[0] for item in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])

    def test_draft_mode_switch_keeps_offline_template(self):
        token = self._token("小明", "pw-editor")
        rejected = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(token),
            json={"query": "掌柜示例耳机 X1 的蓝牙版本，写成要点", "draft_mode": "react"},
        )
        self.assertEqual(rejected.status_code, 400)
        created = self.client.post(
            "/api/listing/tasks",
            headers=self._headers(token),
            json={"query": "掌柜示例耳机 X1 的蓝牙版本，写成要点", "draft_mode": "offline"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["draft"]["policy"], "template-draft-conservative")
        self.assertIn("5.3", "\n".join(created.json()["draft"]["bullets"]))


if __name__ == "__main__":
    unittest.main()
