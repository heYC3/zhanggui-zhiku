# 清理第一次导入时的旧数据（文件名兜底版本），只留 GPT 正确识别的那份
import os
import sys

from pymilvus import MilvusClient

uri = os.getenv("MILVUS_URL")
if not uri:
    sys.exit("请设置 MILVUS_URL")
c = MilvusClient(uri=uri)
old_name = "示例-掌柜耳机说明书"

c.delete(collection_name="kb_item_names", filter=f"item_name == '{old_name}'")
c.delete(collection_name="kb_chunks", filter=f"item_name == '{old_name}'")

c.load_collection("kb_item_names")
c.load_collection("kb_chunks")
print("kb_item_names:", c.query("kb_item_names", filter="pk >= 0", output_fields=["count(*)"]))
print("kb_chunks:", c.query("kb_chunks", filter="chunk_id >= 0", output_fields=["count(*)"]))