"""手册解析和图片交付契约；只使用合成资料。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.evidence.manual_assets import publish_image, router
from app.evidence.pdf_text import recover_cover_text
from app.evidence.manual_retrieval import score_document_passages, interleave_products


class ImportTests(unittest.TestCase):
    def test_cover_recovery_uses_pdf_text_and_is_idempotent(self):
        page = type('Page', (), {'extract_text': lambda _: 'QZ908\n合成设备用户手册'})()
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory)/'manual.pdf'
            pdf.write_bytes(b'pdf fixture')
            with patch('pypdf.PdfReader', return_value=type('PDF', (), {'pages': [page]})()):
                recovered, audit = recover_cover_text(pdf, '# 合成设备用户手册')
                self.assertIn('QZ908', recovered)
                self.assertEqual(audit['pdf_page'], 1)
                self.assertEqual(audit['status'], 'supplemented')
                self.assertEqual(recover_cover_text(pdf, recovered)[0], recovered)

    def test_scanned_cover_reports_ocr_gap(self):
        page = type('Page', (), {'extract_text': lambda _: ''})()
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory)/'manual.pdf'
            pdf.write_bytes(b'pdf fixture')
            with patch('pypdf.PdfReader', return_value=type('PDF', (), {'pages': [page]})()):
                text, audit = recover_cover_text(pdf, '# 原文')
                self.assertEqual(audit['status'], 'needs_ocr')
                self.assertEqual(text, '# 原文')

    def test_image_is_immutable_reachable_and_directory_is_not_public(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MANUAL_ASSET_DIR': directory}):
            source = Path(directory)/'source.png'
            Image.new('RGB', (12, 9), 'red').save(source)
            first = publish_image(source)
            Image.new('RGB', (12, 9), 'blue').save(source)
            second = publish_image(source)
            self.assertNotEqual(first, second)
            app = FastAPI()
            app.include_router(router)
            with TestClient(app) as client:
                response = client.get(first)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'image/png')
                self.assertEqual(client.get('/manual-assets/source.png').status_code, 404)
                self.assertEqual(client.get('/manual-assets/'+'0'*64+'.png').status_code, 404)
                self.assertEqual(client.get('/manual-assets/..%2Fsource.png').status_code, 404)

    def test_long_table_passage_can_win_without_changing_evidence(self):
        raw = '长表\n' + '\n'.join('无关参数 | 很长的其他内容'*10 for _ in range(20)) + '\n输入容量 | 73页\n附注'
        docs = [{'item_name': 'A', 'chunk_id': 1, 'content': raw},
                {'item_name': 'A', 'chunk_id': 2, 'content': '容量操作提示'},
                {'item_name': 'B', 'chunk_id': 3, 'content': '输入容量 | 19页'}]
        def rerank(query, views):
            return [0.95 if '73页' in v and len(v) < 400 else 0.5 for v in views]
        result = score_document_passages('输入容量', docs, rerank)
        self.assertEqual(result[0]['content'], raw)
        self.assertEqual(result[0]['chunk_id'], 1)
        self.assertEqual([d['item_name'] for d in interleave_products(result, ['A','B'])][:2], ['A','B'])

    def test_final_images_only_come_from_actual_context(self):
        with patch.dict(os.environ, {'LLM_DEFAULT_TEMPERATURE':'0.0','CHAT_HISTORY_ENABLE':'false',
                                     'LOG_CONSOLE_ENABLE':'false','LOG_FILE_ENABLE':'false',
                                     'PROJECT_ROOT': str(Path(__file__).resolve().parents[2])}):
            from app.query_process.agent.nodes.node_answer_output import NodeAnswerOutput
        module='app.query_process.agent.nodes.node_answer_output'
        state=dict(answer='合成答案',is_stream=True,session_id='synthetic-image-context',
                   context_docs=[{'content':'![图](/manual-assets/used.png)'}],
                   reranked_docs=[{'content':'![未进入提示词的图](/manual-assets/excluded.png)'}])
        with patch.object(NodeAnswerOutput,'_step_4_write_history'), \
             patch(module+'.add_done_task'),patch(module+'.set_task_result'), \
             patch(module+'.push_to_session') as push:
            result=NodeAnswerOutput().process(state)
        self.assertEqual(result['image_urls'],['/manual-assets/used.png'])
        self.assertEqual(push.call_args.args[2]['image_urls'],result['image_urls'])


if __name__ == '__main__':
    unittest.main()
