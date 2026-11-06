"""真实模型草稿节点。用脚本客户端代替网络，离线回归不在这里调用付费模型。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.listing_agent.drafting import build_draft
from app.listing_agent.llm_compare import compare_generators, write_compare
from app.listing_agent.llm_draft import (
    FAILURE_EMPTY,
    FAILURE_NO_EVIDENCE,
    FAILURE_PARSE,
    FAILURE_TIMEOUT,
    FAILURE_TRANSPORT,
    DraftCallError,
    ModelReply,
    generate_draft_with_fallback,
    generate_llm_draft,
    resolve_draft_mode,
)
from app.listing_agent.paths import EVAL_PATH
from app.listing_agent.retrieve import search_evidence
from app.listing_agent.service import ListingError, ListingService


_SENTENCE = "蓝牙版本是 5.3，打开盒盖就能和手机配对。"


class ScriptedClient:
    def __init__(
        self,
        text: str = "",
        *,
        model: str = "scripted-model",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        error: Exception | None = None,
    ):
        self.text = text
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.error = error
        self.calls = 0
        self.messages: list = []

    def complete(self, *, messages, timeout_s):
        self.calls += 1
        self.messages.append(messages)
        if self.error:
            raise self.error
        return ModelReply(
            text=self.text,
            model=self.model,
            model_name_source="response",
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            parameters={"temperature": 1, "timeout_s": timeout_s, "response_format": "json_object"},
        )


def _grounded() -> str:
    return json.dumps(
        {
            "title": f"掌柜示例X1 {_SENTENCE}",
            "bullets": [_SENTENCE],
            "gaps": [],
        },
        ensure_ascii=False,
    )


class LlmDraftTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._price_keys = (
            "LISTING_LLM_USD_PER_MILLION_PROMPT",
            "LISTING_LLM_USD_PER_MILLION_COMPLETION",
            "LISTING_LLM_PROVIDER",
        )
        self._saved = {key: os.environ.get(key) for key in self._price_keys}
        for key in self._price_keys:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _service(self, client=None, clients=None) -> ListingService:
        return ListingService(Path(self._tmp.name), llm_client=client, llm_clients=clients)

    def test_mode_switch_names(self):
        self.assertEqual(resolve_draft_mode("offline"), "offline")
        self.assertEqual(resolve_draft_mode("LLM"), "llm")
        with self.assertRaises(ValueError):
            resolve_draft_mode("react")

    def test_llm_draft_waits_for_confirmation(self):
        client = ScriptedClient(_grounded(), prompt_tokens=11, completion_tokens=7, total_tokens=18)
        service = self._service(client)
        try:
            created = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                user_id="小明",
                role="editor",
                draft_mode="llm",
            )
            self.assertEqual(created["status"], "awaiting_confirmation")
            self.assertEqual(created["draft"]["policy"], "llm-draft")
            self.assertEqual(created["draft"]["bullets"], [_SENTENCE])
            self.assertTrue(created["validation"]["ok"])
            self.assertEqual(created["draft"]["call"]["model"], "scripted-model")
            self.assertEqual(created["draft"]["call"]["prompt_version"], "listing-draft-v1")
            self.assertEqual(created["draft"]["call"]["prompt_tokens"], 11)
            self.assertTrue(created["draft"]["call"]["usage_known"])
            self.assertIsNone(created["draft"]["call"]["cost"])
            os.environ["LISTING_LLM_USD_PER_MILLION_PROMPT"] = "1"
            os.environ["LISTING_LLM_USD_PER_MILLION_COMPLETION"] = "2"
            priced = generate_llm_draft(
                "掌柜示例耳机 X1 的蓝牙版本",
                "zg-ear-x1",
                created["evidence"],
                "amazon",
                client=ScriptedClient(_grounded(), prompt_tokens=11, completion_tokens=7),
            )
            self.assertEqual(priced.telemetry["cost"], round(11 / 1_000_000 + 7 * 2 / 1_000_000, 8))
            prompt = client.messages[0][0]["content"] + client.messages[0][1]["content"]
            self.assertIn("不能补写", prompt)
            self.assertIn(_SENTENCE, prompt)
            self.assertIn("title", prompt)
            written = service.confirm(created["task_id"], "小明", "editor", created["draft_version"])
            self.assertEqual(written["status"], "written")
        finally:
            service.close()

    def test_unknown_usage_is_not_zero(self):
        client = ScriptedClient(_grounded())
        service = self._service(client)
        try:
            created = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                user_id="小明",
                role="editor",
                draft_mode="llm",
            )
            call = created["draft"]["call"]
            self.assertIsNone(call["prompt_tokens"])
            self.assertIsNone(call["completion_tokens"])
            self.assertIsNone(call["cost"])
            self.assertFalse(call["usage_known"])
            log = Path(self._tmp.name) / "draft_telemetry.jsonl"
            saved = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
            encoded = json.dumps(saved, ensure_ascii=False)
            self.assertIn('"prompt_tokens": null', encoded)
            self.assertIn('"cost": null', encoded)
            self.assertNotIn('"prompt_tokens": 0', encoded)
            self.assertNotIn('"cost": 0', encoded)
        finally:
            service.close()

    def test_rules_block_ungrounded_bullet_until_human_fixes_it(self):
        client = ScriptedClient(
            json.dumps(
                {"title": "掌柜示例X1 普通耳机", "bullets": ["这款耳机完全防水。"], "gaps": []},
                ensure_ascii=False,
            )
        )
        service = self._service(client)
        try:
            created = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                user_id="小明",
                role="editor",
                draft_mode="llm",
            )
            self.assertEqual(created["status"], "awaiting_confirmation")
            self.assertFalse(created["validation"]["ok"])
            with self.assertRaises(ListingError):
                service.confirm(created["task_id"], "小明", "editor", created["draft_version"])
        finally:
            service.close()

    def test_parse_timeout_empty_and_missing_evidence(self):
        evidence = search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1", strategy="sparse")["evidence"]
        parsed = generate_llm_draft(
            "蓝牙",
            "zg-ear-x1",
            evidence,
            "amazon",
            client=ScriptedClient("不是 json"),
        )
        self.assertIsNone(parsed.draft)
        self.assertEqual(parsed.telemetry["failure_type"], FAILURE_PARSE)
        self.assertIsNone(parsed.telemetry["cost"])

        extra = generate_llm_draft(
            "蓝牙",
            "zg-ear-x1",
            evidence,
            "amazon",
            client=ScriptedClient(
                json.dumps(
                    {"title": f"掌柜示例X1 {_SENTENCE}", "bullets": [_SENTENCE], "waterproof": True},
                    ensure_ascii=False,
                )
            ),
        )
        self.assertEqual(extra.telemetry["failure_type"], FAILURE_PARSE)

        empty = generate_llm_draft(
            "蓝牙",
            "zg-ear-x1",
            evidence,
            "amazon",
            client=ScriptedClient("  "),
        )
        self.assertEqual(empty.telemetry["failure_type"], FAILURE_EMPTY)
        self.assertIsNone(empty.telemetry["prompt_tokens"])

        timed = generate_llm_draft(
            "蓝牙",
            "zg-ear-x1",
            evidence,
            "amazon",
            client=ScriptedClient(error=DraftCallError(FAILURE_TIMEOUT, "模型超时", model="requested-model")),
        )
        self.assertEqual(timed.telemetry["failure_type"], FAILURE_TIMEOUT)
        self.assertEqual(timed.telemetry["model"], "requested-model")
        self.assertIsNone(timed.telemetry["prompt_tokens"])
        self.assertIsNone(timed.telemetry["cost"])

        watcher = ScriptedClient(_grounded())
        missing = generate_llm_draft("蓝牙", "zg-ear-x1", [], "amazon", client=watcher)
        self.assertEqual(missing.telemetry["failure_type"], FAILURE_NO_EVIDENCE)
        self.assertEqual(watcher.calls, 0)
        self.assertEqual(missing.telemetry["cost"], 0)
        self.assertEqual(missing.telemetry["usage_source"], "not_called")

        service = self._service(ScriptedClient(error=TimeoutError("超时")))
        try:
            failed = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                user_id="小明",
                role="editor",
                draft_mode="llm",
            )
            self.assertEqual(failed["status"], "awaiting_confirmation")
            self.assertEqual(failed["draft"]["policy"], "template-draft-conservative")
            self.assertIn("知识库", failed["draft"]["fallback_note"])
            self.assertIsNone(failed["draft"]["call"]["attempts"][0]["cost"])
            self.assertIsNone(failed["draft"]["call"]["attempts"][0]["prompt_tokens"])
            self.assertEqual(failed["draft"]["call"]["cost"], 0)
            self.assertEqual(failed["draft"]["call"]["usage_source"], "not_called")
            written = service.confirm(failed["task_id"], "小明", "editor", failed["draft_version"])
            self.assertEqual(written["status"], "written")
        finally:
            service.close()

    def test_provider_fallback_then_confirm(self):
        os.environ["LISTING_LLM_PROVIDER"] = "openai"
        openai = ScriptedClient(error=DraftCallError(FAILURE_TIMEOUT, "模型超时"))
        bailian = ScriptedClient(_grounded(), model="qwen-plus")
        service = self._service(clients={"openai": openai, "bailian": bailian})
        try:
            created = service.start(
                "掌柜示例耳机 X1 的蓝牙版本，写成要点",
                user_id="小明",
                role="editor",
                draft_mode="llm",
            )
            self.assertEqual(openai.calls, 1)
            self.assertEqual(bailian.calls, 1)
            self.assertEqual(created["status"], "awaiting_confirmation")
            self.assertEqual(created["draft"]["call"]["provider"], "bailian")
            self.assertEqual(created["draft"]["call"]["model"], "qwen-plus")
            self.assertIn("百炼", created["draft"]["fallback_note"])
            self.assertTrue(created["validation"]["ok"])
            written = service.confirm(created["task_id"], "小明", "editor", created["draft_version"])
            self.assertEqual(written["status"], "written")
        finally:
            service.close()

        os.environ["LISTING_LLM_PROVIDER"] = "bailian"
        first = ScriptedClient(error=DraftCallError(FAILURE_TRANSPORT, "百炼没连上"))
        second = ScriptedClient(_grounded(), model="gpt-6.1-sol")
        switched = generate_draft_with_fallback(
            "掌柜示例耳机 X1 的蓝牙版本",
            "zg-ear-x1",
            search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1", strategy="sparse")["evidence"],
            "amazon",
            clients={"bailian": first, "openai": second},
            allow_env=False,
        )
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        self.assertEqual(switched.telemetry["provider"], "openai")
        self.assertIsNone(switched.telemetry["attempts"][0]["cost"])

        both = generate_draft_with_fallback(
            "掌柜示例耳机 X1 的蓝牙版本",
            "zg-ear-x1",
            search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1", strategy="sparse")["evidence"],
            "amazon",
            clients={
                "openai": ScriptedClient(error=TimeoutError("超时")),
                "bailian": ScriptedClient("不是 json"),
            },
            allow_env=False,
        )
        self.assertEqual(both.draft["policy"], "template-draft-conservative")
        self.assertEqual(both.telemetry["provider"], "knowledge")
        self.assertIsNone(both.telemetry["attempts"][0]["prompt_tokens"])
        self.assertIsNone(both.telemetry["attempts"][1]["cost"])
        self.assertEqual(both.telemetry["cost"], 0)

    def test_compare_table_marks_unknown_model_cost(self):
        client = ScriptedClient(_grounded())
        tasks = [
            task
            for task in json.loads(EVAL_PATH.read_text(encoding="utf-8"))["tasks"]
            if task["id"] == "draft-x1-link"
        ]
        report = compare_generators(tasks, client=client)
        row = report["rows"][0]
        self.assertTrue(row["offline"]["factual"])
        self.assertEqual(row["offline"]["cost"], 0)
        self.assertTrue(row["llm"]["factual"])
        self.assertIsNone(row["llm"]["cost"])
        self.assertIsNotNone(row["llm"]["language"])
        self.assertIsNotNone(row["offline"]["language"])
        paths = write_compare(report, Path(self._tmp.name))
        text = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("模板事实", text)
        self.assertIn("未知", text)
        logged = [json.loads(line) for line in paths["telemetry"].read_text(encoding="utf-8").splitlines()]
        llm_log = next(item for item in logged if item["generator"] == "llm")
        self.assertIsNone(llm_log["cost"])
        self.assertIsNone(llm_log["prompt_tokens"])
        offline = build_draft("掌柜示例耳机 X1 的蓝牙版本，写成要点", "zg-ear-x1", search_evidence("掌柜示例耳机 X1 的蓝牙版本", sku="zg-ear-x1")["evidence"], "template-draft-conservative")
        self.assertIn("5.3", "\n".join(offline["bullets"]))
