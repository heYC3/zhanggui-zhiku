"""只模拟构造过程验证加载锁，不下载模型或导入PyTorch。"""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.lm import embedding_utils


class EmbeddingInitializationTests(unittest.TestCase):
    def test_parallel_first_queries_construct_and_warm_only_one_model(self):
        gate=threading.Barrier(5)
        model=object()
        def initialize():
            time.sleep(0.03)
            embedding_utils._bge_m3_ef=model
            return model
        def query(_):
            gate.wait(timeout=5)
            return embedding_utils.get_bge_m3_ef()
        with patch.object(embedding_utils,'_bge_m3_ef',None), \
             patch.object(embedding_utils,'_initialize_bge_model',side_effect=initialize) as load:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results=list(pool.map(query,range(5)))
            self.assertEqual(load.call_count,1)
            self.assertTrue(all(result is model for result in results))


if __name__=='__main__':unittest.main()
