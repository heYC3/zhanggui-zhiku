"""故障窗口、并发和 SSE。进程用强制结束，不用正常退出冒充。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.listing_agent.paths import PACKAGE_ROOT
from app.listing_agent.service import ListingError, ListingService


_QUERY = "掌柜示例耳机 X1 的蓝牙版本，写成要点"


def _worker(runtime: Path, extra: dict) -> subprocess.Popen:
    env = os.environ.copy()
    env["LISTING_RUNTIME_DIR"] = str(runtime)
    env["LISTING_DRAFT_MODE"] = "offline"
    for key in ("LISTING_CRASH_AT", "LISTING_CHECKPOINT", "LISTING_GO", "LISTING_FAULT_SLEEP"):
        env.pop(key, None)
    env.update(extra)
    return subprocess.Popen(
        [sys.executable, "-m", "app.listing_agent.fault_worker"],
        cwd=str(PACKAGE_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_line(process: subprocess.Popen, timeout: float = 30) -> str:
    holder: list[str] = []

    def target() -> None:
        holder.append(process.stdout.readline() if process.stdout else "")

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        process.kill()
        thread.join(2)
        err = process.stderr.read() if process.stderr else ""
        raise TimeoutError(err[-500:])
    line = holder[0].strip()
    if not line:
        err = process.stderr.read() if process.stderr else ""
        raise RuntimeError(err[-800:] or "工作进程没有输出")
    return line


def _kill(process: subprocess.Popen) -> int:
    process.kill()
    code = process.wait(timeout=10)
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()
    return code


class DurabilityTest(unittest.TestCase):
    def test_kill_before_confirm_then_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            process = _worker(runtime, {"LISTING_FAULT_ACTION": "start"})
            try:
                payload = json.loads(_read_line(process))
                self.assertEqual(payload["status"], "awaiting_confirmation")
                code = _kill(process)
            finally:
                if process.poll() is None:
                    process.kill()
            self.assertNotEqual(code, 0)
            time.sleep(0.4)
            restarted = ListingService(runtime)
            try:
                current = restarted.snapshot(payload["task_id"])
                self.assertEqual(current["status"], "awaiting_confirmation")
                self.assertEqual(restarted.ledger.listing_count(payload["task_id"]), 0)
                written = restarted.confirm(
                    payload["task_id"], "小明", "editor", int(payload["version"])
                )
                again = None
                try:
                    restarted.confirm(payload["task_id"], "小明", "editor", int(payload["version"]))
                except ListingError as exc:
                    again = str(exc)
                self.assertEqual(written["status"], "written")
                self.assertEqual(restarted.ledger.listing_count(payload["task_id"]), 1)
                self.assertIsNotNone(again)
            finally:
                restarted.close()

    def test_kill_after_write_then_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            starter = _worker(runtime, {"LISTING_FAULT_ACTION": "start", "LISTING_FAULT_SLEEP": "0.2"})
            first = json.loads(_read_line(starter))
            starter.wait(timeout=10)
            process = _worker(
                runtime,
                {
                    "LISTING_FAULT_ACTION": "confirm",
                    "LISTING_FAULT_TASK": first["task_id"],
                    "LISTING_FAULT_VERSION": str(first["version"]),
                },
            )
            try:
                payload = json.loads(_read_line(process))
                self.assertEqual(payload["status"], "written")
                code = _kill(process)
            finally:
                if process.poll() is None:
                    process.kill()
            self.assertNotEqual(code, 0)
            time.sleep(0.4)
            restarted = ListingService(runtime)
            try:
                current = restarted.snapshot(first["task_id"])
                self.assertEqual(current["status"], "written")
                self.assertEqual(restarted.ledger.listing_count(first["task_id"]), 1)
                with self.assertRaises(ListingError):
                    restarted.confirm(first["task_id"], "小明", "editor", int(first["version"]))
                self.assertEqual(restarted.ledger.listing_count(first["task_id"]), 1)
            finally:
                restarted.close()

    def test_crash_before_sqlite_checkpoint_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            process = _worker(
                runtime,
                {"LISTING_FAULT_ACTION": "start", "LISTING_CRASH_AT": "before_checkpoint_save"},
            )
            code = process.wait(timeout=30)
            self.assertEqual(code, 86)
            restarted = ListingService(runtime)
            try:
                rows = restarted.ledger._conn.execute("SELECT status FROM tasks").fetchall()
                self.assertTrue(rows)
                self.assertEqual(rows[0]["status"], "running")
                self.assertEqual(restarted.ledger.listing_count(), 0)
            finally:
                restarted.close()

    def test_crash_before_file_snapshot_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            process = _worker(
                runtime,
                {
                    "LISTING_FAULT_ACTION": "start",
                    "LISTING_CHECKPOINT": "file",
                    "LISTING_CRASH_AT": "before_checkpoint_save",
                },
            )
            code = process.wait(timeout=30)
            self.assertEqual(code, 86)
            os.environ["LISTING_CHECKPOINT"] = "file"
            try:
                restarted = ListingService(runtime)
                try:
                    row = restarted.ledger._conn.execute(
                        "SELECT task_id, status, draft_version FROM tasks"
                    ).fetchone()
                    self.assertEqual(row["status"], "awaiting_confirmation")
                    with self.assertRaises(ListingError):
                        restarted.confirm(row["task_id"], "小明", "editor", int(row["draft_version"] or 0))
                    self.assertEqual(restarted.ledger.listing_count(row["task_id"]), 0)
                finally:
                    restarted.close()
            finally:
                os.environ.pop("LISTING_CHECKPOINT", None)

    def test_two_processes_confirm_once(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            service = ListingService(runtime)
            created = service.start(_QUERY, user_id="小明", role="editor", draft_mode="offline")
            service.close()
            go = runtime / "go"
            extra = {
                "LISTING_FAULT_ACTION": "confirm",
                "LISTING_FAULT_TASK": created["task_id"],
                "LISTING_FAULT_VERSION": str(created["draft_version"]),
                "LISTING_GO": str(go),
                "LISTING_FAULT_SLEEP": "0.1",
            }
            first = _worker(runtime, extra)
            second = _worker(runtime, extra)
            time.sleep(0.3)
            go.write_text("go", encoding="utf-8")
            first.wait(timeout=30)
            second.wait(timeout=30)
            checked = ListingService(runtime)
            try:
                self.assertEqual(checked.ledger.listing_count(created["task_id"]), 1)
                self.assertEqual(checked.snapshot(created["task_id"])["status"], "written")
            finally:
                checked.close()

    def test_edit_and_confirm_race_writes_at_most_once(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            service = ListingService(runtime)
            created = service.start(_QUERY, user_id="小明", role="editor", draft_mode="offline")
            service.close()
            go = runtime / "go"
            common = {
                "LISTING_FAULT_TASK": created["task_id"],
                "LISTING_FAULT_VERSION": str(created["draft_version"]),
                "LISTING_GO": str(go),
                "LISTING_FAULT_SLEEP": "0.1",
            }
            editing = _worker(runtime, {**common, "LISTING_FAULT_ACTION": "edit"})
            confirming = _worker(runtime, {**common, "LISTING_FAULT_ACTION": "confirm"})
            time.sleep(0.3)
            go.write_text("go", encoding="utf-8")
            editing.wait(timeout=30)
            confirming.wait(timeout=30)
            checked = ListingService(runtime)
            try:
                self.assertLessEqual(checked.ledger.listing_count(created["task_id"]), 1)
            finally:
                checked.close()

    def test_two_users_do_not_see_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            left = right = None
            left = _worker(
                runtime,
                {
                    "LISTING_FAULT_ACTION": "start",
                    "LISTING_FAULT_USER": "甲",
                    "LISTING_FAULT_QUERY": "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                    "LISTING_FAULT_SLEEP": "2",
                },
            )
            right = _worker(
                runtime,
                {
                    "LISTING_FAULT_ACTION": "start",
                    "LISTING_FAULT_USER": "乙",
                    "LISTING_FAULT_QUERY": "掌柜示例移动电源 P20 的容量，写成要点",
                    "LISTING_FAULT_SLEEP": "2",
                },
            )
            try:
                left_body = json.loads(_read_line(left))
                right_body = json.loads(_read_line(right))
                left.wait(timeout=15)
                right.wait(timeout=15)
                self.assertNotEqual(left_body["task_id"], right_body["task_id"])
                service = ListingService(runtime)
                try:
                    with self.assertRaises(ListingError):
                        service.snapshot_for(left_body["task_id"], "乙")
                    with self.assertRaises(ListingError):
                        service.snapshot_for(right_body["task_id"], "甲")
                finally:
                    service.close()
            finally:
                for process in (left, right):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

    def test_node_event_is_stored_before_the_request_returns(self):
        os.environ["LISTING_PAUSE_AFTER_NODE"] = "resolve_product"
        os.environ["LISTING_PAUSE_SECONDS"] = "1.2"
        try:
            with tempfile.TemporaryDirectory() as directory:
                service = ListingService(Path(directory))
                seen: list[str] = []

                def run() -> None:
                    service.start(_QUERY, user_id="小明", role="editor", draft_mode="offline")

                thread = threading.Thread(target=run)
                thread.start()
                deadline = time.time() + 5
                while time.time() < deadline and thread.is_alive():
                    rows = service.ledger._conn.execute(
                        "SELECT data_json FROM events"
                    ).fetchall()
                    for row in rows:
                        if "resolve_product" in row["data_json"]:
                            seen.append(row["data_json"])
                    if seen:
                        break
                    time.sleep(0.05)
                thread.join(timeout=10)
                service.close()
                self.assertTrue(seen)
        finally:
            os.environ.pop("LISTING_PAUSE_AFTER_NODE", None)
            os.environ.pop("LISTING_PAUSE_SECONDS", None)


class StreamCancelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LISTING_RUNTIME_DIR"] = self._tmp.name
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
        store.close()
        self.api = api
        self.client = TestClient(api.app)
        token = self.client.post(
            "/api/listing/sessions",
            json={"user_id": "小明", "password": "pw-editor"},
        ).json()["token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        if self.api._service is not None:
            self.api._service.close()
            self.api._service = None
        if self.api._auth is not None:
            self.api._auth.close()
            self.api._auth = None
        self._tmp.cleanup()

    def test_closing_the_stream_does_not_cancel_the_task(self):
        import asyncio

        from app.listing_agent.api import iter_listing_events

        created = self.client.post(
            "/api/listing/tasks",
            headers=self.headers,
            json={"query": _QUERY, "draft_mode": "offline"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        task_id = created.json()["task_id"]
        seen = {"n": 0}

        async def disconnected() -> bool:
            seen["n"] += 1
            return seen["n"] > 1

        async def read_until_disconnect() -> int:
            count = 0
            async for _chunk in iter_listing_events(
                self.api.get_service(), task_id, "小明", disconnected
            ):
                count += 1
            return count

        count = asyncio.run(asyncio.wait_for(read_until_disconnect(), timeout=3))
        self.assertGreater(count, 0)
        current = self.client.get(f"/api/listing/tasks/{task_id}", headers=self.headers)
        self.assertEqual(current.json()["status"], "awaiting_confirmation")
        cancelled = self.client.post(
            f"/api/listing/tasks/{task_id}/cancel",
            headers=self.headers,
            json={},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        names = [item["data"].get("name") for item in cancelled.json()["events"]]
        self.assertIn("cancel", names)
        denied = self.client.post(
            f"/api/listing/tasks/{task_id}/confirm",
            headers=self.headers,
            json={"draft_version": created.json()["draft_version"]},
        )
        self.assertEqual(denied.status_code, 400)


if __name__ == "__main__":
    unittest.main()
