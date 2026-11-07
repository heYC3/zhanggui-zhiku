"""检索修复的合成契约，不含真实手册、题目或答案。"""

import copy
import unittest

from app.evidence.manual_retrieval import (candidate_union, cover_products, document_key,
    fuse_by_product, fuse_rankings, lexical_rank, score_original_documents, search_text)


def doc(product, cid, text='合成资料'):
    return {'item_name': product, 'chunk_id': cid, 'content': text, 'source': 'local'}


class ManualRetrievalTests(unittest.TestCase):
    def test_table_fragment_does_not_lose_complete_rows_or_unclosed_cell(self):
        self.assertIn('属性 | 17W', search_text('<table><tr><td>属性</td><td>17W</td></tr>'))
        self.assertIn('属性 | 17W', search_text('<tr><td>属性</td><td>17W'))

    def test_rowspan_values_stay_attached_to_later_sizes(self):
        raw = '<table><tr><td>介质</td><td>容量</td><td>尺寸</td></tr><tr><td rowspan="2">卡片</td><td rowspan="2">7张</td><td>S1</td></tr><tr><td>S2</td></tr></table>'
        self.assertIn('卡片 | 7张 | S2', search_text(raw))

    def test_multiple_tables_do_not_share_rowspan_state(self):
        raw = '<table><tr><td rowspan="2">甲</td><td>1</td></tr></table><table><tr><td>乙</td><td>2</td></tr></table>'
        self.assertIn('乙 | 2', search_text(raw))
        self.assertNotIn('甲 | 乙', search_text(raw))

    def test_colspan_and_rowspan_preserve_column_alignment(self):
        raw = '<table><tr><td rowspan="2">甲</td><td colspan="2">组</td></tr><tr><td>左</td><td>右</td></tr></table>'
        self.assertIn('甲 | 左 | 右', search_text(raw))

    def test_footnote_and_steps_survive_image_removal(self):
        raw = '先展开材料。\n![模型生成的图片转述](http://internal/image.jpg)\n*桌面客户端支持；移动端不支持。'
        text = search_text(raw)
        self.assertIn('先展开材料', text)
        self.assertIn('移动端不支持', text)
        self.assertNotIn('模型生成', text)
        self.assertNotIn('http', text)

    def test_lexical_channel_can_recover_specific_medium_from_app_noise(self):
        docs = [doc('A', i, '应用程序安装登录配置应用设置') for i in range(10)]
        target = doc('A', 'spec', '<table><tr><td>卡片介质</td><td>7张</td></tr></table>桌面程序支持卡片，移动程序不支持。')
        result = lexical_rank('卡片介质容量是多少，移动程序能打印卡片吗？', docs+[target])
        self.assertEqual(result[0]['chunk_id'], 'spec')

    def test_zero_overlap_is_not_an_arbitrary_lexical_hit(self):
        self.assertEqual(lexical_rank('额定功耗', [doc('A', 1, '无线网络')]), [])

    def test_candidate_union_keeps_each_channels_strongest_evidence(self):
        semantic = [doc('A', i) for i in range(12)]
        lexical = [doc('A', 'new')] + semantic[3:]
        result = candidate_union([semantic, lexical], 6)
        self.assertEqual(len(result), 6)
        self.assertIn('new', [x['chunk_id'] for x in result])
        self.assertIn(0, [x['chunk_id'] for x in result])

    def test_same_chunk_id_in_different_products_does_not_merge(self):
        result = fuse_rankings([[doc('A', 1), doc('B', 1)], [doc('A', 1)]])
        self.assertEqual(len(result), 2)
        self.assertEqual({x['item_name'] for x in result}, {'A', 'B'})

    def test_missing_ids_use_full_text_identity(self):
        self.assertNotEqual(document_key(doc('A', None, '甲')), document_key(doc('A', None, '乙')))
        self.assertEqual(len(fuse_rankings([[doc('A', None, '甲')]*2])), 1)

    def test_product_quota_survives_fusion_and_product_order_changes(self):
        first = [doc('A', i) for i in range(12)] + [doc('B', i) for i in range(12)]
        for names in [['A','B'], ['B','A']]:
            result = fuse_by_product([first, first], names, per_product=8)
            self.assertEqual(len(result), 16)
            self.assertEqual([x['item_name'] for x in result[:2]], names)
            self.assertEqual(sum(x['item_name']=='A' for x in result), 8)

    def test_rerank_uses_readable_text_but_returns_unmodified_evidence(self):
        docs = [doc('A', 1, '<table><tr><td>额定功耗</td><td>9W</td></tr></table>')]
        original = copy.deepcopy(docs)
        def rerank(query, texts):
            self.assertIn('额定功耗 | 9W', texts[0])
            self.assertNotIn('<td>', texts[0])
            return [0.8]
        result = score_original_documents('额定功耗', docs, rerank)
        self.assertEqual(result[0]['content'], docs[0]['content'])
        self.assertEqual(docs, original)

    def test_incomplete_or_nonfinite_scores_cannot_silently_drop_documents(self):
        for scores in [[], [float('nan')], [float('inf')]]:
            with self.assertRaises(ValueError):
                score_original_documents('问题', [doc('A', 1)], lambda *_: scores)

    def test_final_context_reserves_other_product_after_cliff_cutoff(self):
        ranked = [doc('A', i) for i in range(10)] + [doc('B', 'spec')]
        selected = cover_products(ranked[:3], ranked, ['A', 'B'], 3)
        self.assertEqual(len(selected), 3)
        self.assertEqual([x['item_name'] for x in selected[:2]], ['A', 'B'])
        self.assertEqual(selected[1]['chunk_id'], 'spec')


if __name__ == '__main__':
    unittest.main()
