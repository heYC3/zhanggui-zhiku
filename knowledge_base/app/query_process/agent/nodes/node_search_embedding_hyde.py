from app.clients.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search
from app.conf.milvus_config import milvus_config
from app.core.load_prompt import load_prompt
from app.lm.embedding_utils import generate_embeddings
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.node_base import NodeBase
from app.core.logger import logger
from app.query_process.agent.state import QueryGraphState, create_default_state
from app.utils.task_utils import add_done_task


class NodeSearchEmbeddingHyde(NodeBase):
    """
    节点功能：HyDE (Hypothetical Document Embedding)
    先让 LLM 生成假设性答案，再对答案进行向量检索，提高召回率。
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_search_embedding_hyde"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        HyDE (Hypothetical Document Embedding) 检索节点
        核心思想：通过LLM生成假设性答案（HyDE文档），将其向量化后用于检索，以解决短查询语义稀疏问题。

        执行步骤：
        1. 参数提取：从会话状态中获取改写后的查询（rewritten_query）和已确认的商品名（item_names）。
        2. 生成假设文档 (Step 1)：调用LLM，基于用户问题生成一段假设性的理想回答（即HyDE文档）。
        3. 混合检索 (Step 2)：
           - 将“用户问题 + 假设文档”合并，生成BGE-M3稠密+稀疏向量。
           - 在Milvus中执行混合检索（带商品名过滤），召回最相似的知识切片。
        4. 结果封装：返回检索到的切片列表和生成的假设文档，更新会话状态。

        :param state: 会话状态字典，包含 session_id, rewritten_query, item_names 等
        :return: 包含 hyde_embedding_chunks (检索结果) 和 hyde_doc (假设文档) 的字典
        """

        # 1、用户问题和已确认商品名
        rewritten_query = state.get("rewritten_query")
        item_names = state.get("item_names")

        try:

            # 2、生成假设性文档
            hyde_doc = self._step_1_create_hyde_doc(rewritten_query)

            # 3、用“重写问题 + 假设文档”检索切片
            hits = self._step_2_search_embedding_hyde(
                rewritten_query=rewritten_query,
                hyde_doc=hyde_doc,
                item_names=item_names,
                top_k=5,
            )

            add_done_task(state["session_id"], self.name)
            return {
                "hyde_embedding_chunks": hits,
                "hyde_doc": hyde_doc,
                "hyde_error": "",
            }

        except Exception as e:
            logger.exception(f"假设性文档向量搜索失败: {e}")
            return {"hyde_embedding_chunks": [], "hyde_error": type(e).__name__}

    def _step_1_create_hyde_doc(self, rewritten_query: str) -> str:
        """
        阶段1：利用大模型根据用户查询生成假设性文档（Hypothetical Document）。
        HyDE的核心在于：利用LLM生成一个“虚构但相关”的文档，用该文档的向量去检索真实的文档，
        从而缓解短查询（Query）与长文档（Document）在语义空间不匹配的问题。

        :param rewritten_query: 用户改写后的查询语句
        :return: LLM生成的假设性文档内容
        """

        logger.info("步骤1: 开始生成假设性文档")

        try:
            llm = get_llm_client()
            # 加载提示词模板，生成假设文档
            hyde_prompt = load_prompt("hyde_prompt", rewritten_query=rewritten_query)
            logger.debug(f"步骤1: Prompt加载成功")

            # 调用LLM生成
            response = llm.invoke(hyde_prompt)
            hyde_doc = response.content

            logger.info(f"步骤1: 假设文档生成完成, 长度: {len(hyde_doc)} 字符")
            logger.debug(f"步骤1: 文档预览: {hyde_doc}")

            return hyde_doc

        except Exception as e:
            logger.exception(f"步骤1: 生成假设文档失败: {e}")
            raise e

    def _step_2_search_embedding_hyde(
            self,
            rewritten_query: str,
            hyde_doc: str,
            item_names=None,
            req_limit: int = 10,
            top_k: int = 5,
            ranker_weights=(0.8, 0.2),  # 调整默认权重以偏向稠密向量 (0.8, 0.2)
            norm_score: bool = True,    # 默认开启归一化
            output_fields=["chunk_id", "content", "item_name"],
    ):
        """
        阶段2：利用“重写问题 + 假设性文档”生成 embedding，并到向量库检索切片。

        :param rewritten_query: 改写后的查询
        :param hyde_doc: Step 1 生成的假设性文档
        :param item_names: 商品名称列表，用于元数据过滤 (item_name in [...])
        :param req_limit: Milvus 搜索时的候选召回数量
        :param top_k: 最终返回的 Top K 结果数量
        :param ranker_weights: 混合检索权重 (Dense, Sparse)
        :param norm_score: 是否对分数进行归一化
        :param output_fields: 返回结果中包含的字段
        :return: 检索结果列表
        """

        try:
            from app.evidence.store import as_query_hit, retrieve_products_evidence
            combined_text = (rewritten_query or "") + " " + (hyde_doc or "")
            found = retrieve_products_evidence(
                combined_text,
                item_names or [],
                strategy="rrf_hyde",
                limit_per_product=top_k,
            )
            return [as_query_hit(row) for row in found]
        except Exception as exc:
            logger.error(f"步骤2: 检索过程发生异常: {exc}")
            raise

if __name__ == "__main__":

    # 当前节点图状态初始值
    init_state = create_default_state(
        session_id = "test_session_002",
        rewritten_query = "如何调整brother HAK180烫金机的转印温度？",
        item_names = ["BrotherHAK180烫金机"],
        is_stream = True
    )

    # 执行节点的业务调用
    node_search_embedding_hyde = NodeSearchEmbeddingHyde()
    final_state = node_search_embedding_hyde(init_state)
