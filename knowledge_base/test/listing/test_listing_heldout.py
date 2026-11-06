"""留出案例。不改冻结测集，也不用原来那 3 个商品的问法。"""

from __future__ import annotations

import json
import unittest

from app.listing_agent.ablation import load_cases, load_products, resolve, score_case
from app.listing_agent.paths import EVAL_PATH


class HeldoutTest(unittest.TestCase):
    def test_cases_are_not_copies_of_the_frozen_set(self):
        frozen = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
        frozen_queries = {task["query"] for task in frozen["tasks"]}
        for case in load_cases():
            self.assertNotIn(case["query"], frozen_queries)
            self.assertNotIn(case["family"], {"掌柜示例耳机", "掌柜示例移动电源"})

    def test_factors_catch_version_conflict_injection_and_gate(self):
        products = load_products()
        scored = {row["id"]: row for row in (score_case(case, products) for case in load_cases() if case["split"] == "test")}
        self.assertTrue(scored["held-version"]["检索命中"])
        self.assertTrue(scored["held-version"]["草稿覆盖"])
        self.assertNotIn("300", scored["held-version"]["禁用词"])
        self.assertTrue(scored["held-units"]["草稿覆盖"])
        self.assertTrue(scored["held-conflict"]["检索命中"])
        self.assertTrue(scored["held-conflict"]["草稿覆盖"])
        self.assertTrue(scored["held-inject"]["草稿覆盖"])
        self.assertNotIn("防水", scored["held-inject"]["禁用词"])
        self.assertEqual(resolve("掌柜留出台灯 L2 的亮度", products)["kind"], "many")
        self.assertTrue(scored["held-ambiguous"]["确认门会停"])
        self.assertFalse(scored["held-ambiguous"]["草稿覆盖"])
