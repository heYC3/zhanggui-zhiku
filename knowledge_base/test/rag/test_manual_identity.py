"""型号边界、上下文及比较问题的回归；全部使用合成资料。"""

import unittest
from unittest.mock import patch

from app.evidence.identity import match_product_names, resolve_product_context, topic_query
from app.evidence.store import retrieve_products_evidence


class ManualIdentityTests(unittest.TestCase):
    catalog = [
        {"item_name": "设备 X1", "aliases": ["X1", "甲设备"]},
        {"item_name": "设备 X1 Pro", "aliases": ["X1 Pro"]},
        {"item_name": "设备 B20", "aliases": ["B20", "乙设备"]},
    ]

    def test_longer_model_is_not_base_model(self):
        self.assertEqual(match_product_names("X1 Pro 支持什么？", self.catalog), (["设备 X1 Pro"], False))
        self.assertEqual(match_product_names("X10 呢？", self.catalog), ([], False))
        self.assertEqual(match_product_names("X1Pro 呢？", self.catalog), (["设备 X1 Pro"], False))

    def test_unknown_variant_does_not_resolve_to_base(self):
        self.assertEqual(match_product_names("X1 Pro 呢？", self.catalog[:1]), ([], False))

    def test_shared_alias_requires_clarification(self):
        catalog = [{"item_name": "A", "aliases": ["通用设备"]},
                   {"item_name": "B", "aliases": ["通用设备"]}]
        self.assertTrue(match_product_names("通用设备功率？", catalog)[1])

    def test_followup_uses_latest_user_product(self):
        history = [{"role": "user", "text": "X1 内存？"},
                   {"role": "user", "text": "B20 内存？"}]
        self.assertEqual(resolve_product_context("待机功率呢？", history, self.catalog)["item_names"], ["设备 B20"])

    def test_explicit_unknown_model_does_not_reuse_history(self):
        result = resolve_product_context("C99 内存呢？", [{"role": "user", "text": "X1 内存？"}], self.catalog)
        self.assertEqual(result["method"], "unknown_model")
        self.assertEqual(result["item_names"], [])

    def test_missing_context_does_not_guess(self):
        self.assertEqual(resolve_product_context("进纸多少张？", [], self.catalog)["method"], "missing_context")

    def test_filtered_search_keeps_topic_and_numeric_constraints(self):
        query = topic_query("关于设备 B20：B20 在 80g 纸下能放 250 页吗？", ["设备 B20"])
        self.assertNotIn("B20", query)
        self.assertIn("80g", query)
        self.assertIn("250", query)
        self.assertIn("B200", topic_query("B200 与 B20 的区别", ["设备 B20"]))

    def test_model_only_search_never_becomes_empty(self):
        self.assertTrue(topic_query("设备 B20", ["设备 B20"]))

    def test_comparison_queries_every_product_and_deduplicates(self):
        def retrieve(query, *, product_id, strategy, limit):
            return {"evidence": [{"product_id": product_id, "chunk_id": "1", "text": product_id}]}
        with patch("app.evidence.store.retrieve_product_evidence", side_effect=retrieve) as search:
            rows = retrieve_products_evidence("容量对比", ["设备 X1", "设备 B20", "设备 X1"], strategy="rrf")
        self.assertEqual(search.call_count, 2)
        self.assertEqual({row["product_id"] for row in rows}, {"设备 X1", "设备 B20"})

    def test_partial_backend_failure_is_not_reported_as_success(self):
        with patch("app.evidence.store.retrieve_product_evidence", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                retrieve_products_evidence("容量", ["设备 X1"], strategy="rrf")


if __name__ == "__main__":
    unittest.main()
