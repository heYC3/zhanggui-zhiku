import unittest

from app.evidence.evaluation import evidence_id, score_retrieval


class EvidenceEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.text = '<table><tr><td>待机功率</td><td>0.217W</td></tr></table>'
        self.key = evidence_id('A1', self.text)
        self.corpus = [{'item_name': 'A1', 'content': self.text, 'evidence_id': self.key}]
        self.question = {'gold_points': [{'label': '待机时耗电零点二一七瓦', 'supporting_chunks': [self.key]}]}

    def test_gold_evidence_is_independent_of_answer_wording_and_table_markup(self):
        result = score_retrieval(self.question, [{'source': 'local', 'content': self.text}], self.corpus)
        self.assertTrue(result['hit_at_1'])
        self.assertEqual(result['point_support_at_3'], [True])

    def test_wrong_product_and_web_content_do_not_receive_credit(self):
        for doc in [{'source': 'web', 'content': self.text},
                    {'source': 'local', 'content': self.text, 'item_name': 'B2'}]:
            self.assertFalse(score_retrieval(self.question, [doc], self.corpus)['hit_at_3'])

    def test_rank_and_multiple_point_coverage_are_distinct(self):
        other = {'source': 'local', 'content': '其他内容'}
        relevant = {'source': 'local', 'content': self.text}
        question = {'gold_points': self.question['gold_points'] + [{'label': '另一要点', 'supporting_chunks': ['different']}]}
        result = score_retrieval(question, [other, relevant], self.corpus)
        self.assertFalse(result['hit_at_1'])
        self.assertTrue(result['hit_at_3'])
        self.assertEqual(result['point_support_at_3'], [True, False])

    def test_identical_text_from_multiple_products_needs_identity(self):
        corpus = self.corpus + [{'item_name': 'B2', 'content': self.text, 'evidence_id': evidence_id('B2', self.text)}]
        result = score_retrieval(self.question, [{'source': 'local', 'content': self.text}], corpus)
        self.assertFalse(result['hit_at_3'])
        self.assertEqual(result['unmapped_local_docs'], 1)

    def test_source_gap_is_explicit(self):
        result = score_retrieval({'gold_points': [{'label': '型号', 'supporting_chunks': []}]}, [], self.corpus)
        self.assertEqual(result['source_gaps'], [0])


if __name__ == '__main__':
    unittest.main()
