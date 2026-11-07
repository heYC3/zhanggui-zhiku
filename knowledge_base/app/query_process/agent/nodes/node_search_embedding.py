
from app.clients.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search
from app.conf.milvus_config import milvus_config
from app.lm.embedding_utils import generate_embeddings
from app.query_process.agent.node_base import NodeBase
from app.core.logger import logger
from app.query_process.agent.state import QueryGraphState, create_default_state
from app.utils.task_utils import add_done_task


class NodeSearchEmbedding(NodeBase):
     """
    节点功能：基于已确认主体名+改写后的用户问题，执行Milvus向量数据库混合检索
    """

     # 覆盖基类的 name 属性，标识节点名称
     name: str = "node_search_embedding"

     def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        核心节点函数：基于已确认商品名+改写后的用户问题，执行Milvus向量数据库混合检索
        流程：用户问题向量化 → 构造带商品名过滤的混合搜索请求 → 执行稠密+稀疏混合检索 → 返回检索结果
        :param state: Dict - 会话状态字典，包含上游传递的核心信息，关键字段：
                      {
                          "session_id": str,        # 会话唯一标识
                          "rewritten_query": str,   # step4改写后的完整用户问题（含商品名）
                          "item_names": list[str],  # step7已确认的标准化商品名列表
                          "is_stream": bool/None    # 是否为流式响应，可选
                      }
        :return: Dict - 检索结果字典，仅包含embedding_chunks字段，供下游节点使用：
                 {
                     "embedding_chunks": List[Dict]  # Milvus检索结果列表，无结果则为空列表
                                                     # 每个元素为一条匹配的向量数据，含业务字段
                 }
        """

        try:
            from app.evidence.store import as_query_hit, retrieve_products_evidence

            query = state.get("rewritten_query") or ""
            item_names = state.get("item_names") or []
            found = retrieve_products_evidence(query, item_names, strategy="rrf", limit_per_product=12)
            add_done_task(state["session_id"], self.name)
            return {"embedding_chunks": [as_query_hit(row) for row in found], "embedding_error": ""}

        except Exception as e:
            logger.exception(f"向量搜索失败: {e}")
            return {"embedding_chunks": [], "embedding_error": type(e).__name__}

if __name__ == "__main__":

    # 当前节点图状态初始值
    init_state = create_default_state(
        session_id = "test_session_002",
        rewritten_query = "如何调整brother HAK180烫金机的转印温度？",
        item_names = ["BrotherHAK180烫金机"],
        is_stream = True
    )

    # 执行节点的业务调用
    node_search_embedding = NodeSearchEmbedding()
    final_state = node_search_embedding(init_state)
