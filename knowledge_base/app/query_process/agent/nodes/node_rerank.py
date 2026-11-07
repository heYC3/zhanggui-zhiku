from typing import List, Dict, Any

from app.lm.reranker_http_utils import rerank_documents
from app.query_process.agent.node_base import NodeBase
from app.core.logger import logger
from app.query_process.agent.state import QueryGraphState
from app.utils.task_utils import add_done_task

# -----------------------------
# Rerank / TopK 全局常量
# -----------------------------
# 动态 TopK 硬上限：最多取前 N 条（<=10）
RERANK_MAX_TOPK: int = 10
# 最小 TopK：至少保留前 N 条（>=1，且 <= RERANK_MAX_TOPK）
RERANK_MIN_TOPK: int = 3 #总数最少条数

# 断崖阈值（绝对，判断高分文档）
RERANK_GAP_ABS: float = 0.5
# 断崖阈值（相对，判断低分文档）
RERANK_GAP_RATIO: float = 0.25



class NodeRerank(NodeBase):
    """
    节点功能：使用 Cross-Encoder 模型对 RRF 后的结果进行精确打分重排。
    流程: 合并多源文档 → Reranker 计算相关性 → 断崖检测动态截断
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_rerank"

    def process(self, state: QueryGraphState) -> QueryGraphState:

        """
        执行重排序
        :param state: 需包含 rrf_chunks、web_search_docs、rewritten_query
        :return: 更新后的 state，包含 reranked_docs
        """

        # 1. 获取 query
        user_query = state.get('rewritten_query', '') or state.get('original_query', '')
        from app.evidence.identity import topic_query
        user_query = topic_query(user_query, state.get('item_names') or [])

        # 2. 合并多源文档
        merged_multi_docs: List[Dict[str, Any]] = self._merge_multi_source_docs(state)

        # 3. Rerank 精排(精排打分)
        self.last_error = ""
        multiple = len(set(state.get('item_names') or [])) > 1
        reranked_docs: List[Dict[str, Any]] = self._rerank_merged_docs(user_query, merged_multi_docs, multiple)

        if len(set(state.get('item_names') or [])) > 1:
            from app.evidence.manual_retrieval import interleave_products
            # 产品间分数不可用来判定截断；每个型号先各交付一个最强片段。
            cutoff_docs = interleave_products(reranked_docs, state['item_names'])[:RERANK_MAX_TOPK]
            add_done_task(state['session_id'], self.name)
            return {'reranked_docs': cutoff_docs, 'rerank_error': self.last_error}

        # 4. 动态 Top_K 截取(断崖检测)
        cutoff_docs = self._cliff_cutoff(reranked_docs)

        add_done_task(state["session_id"], self.name)
        return {"reranked_docs": cutoff_docs, "rerank_error": self.last_error}

    def _cliff_cutoff(self, ranked_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """断崖检测截断：相邻得分差距超过阈值时截断。"""
        if not ranked_docs:
            return []

        upper_bound = min(RERANK_MAX_TOPK, len(ranked_docs))
        lower_bound = min(RERANK_MIN_TOPK, upper_bound)

        # 默认值：取满硬上限（最多10条）
        cutoff_pos = upper_bound

        # 遍历范围：从min_topk-1到max_topk-2（索引从0开始），检测相邻两个文档的分数差
        # 例：min_topk=3，max_topk=10 → 遍历i=2,3,4,5,6,7,8（对应第3~9条文档，检测与下一条的差距）
        for idx in range(lower_bound - 1, upper_bound - 1):
            current_score = ranked_docs[idx].get("score")
            next_score = ranked_docs[idx + 1].get("score")

            if current_score is None or next_score is None:
                continue

            # 计算相邻文档的分数绝对差距（因已降序，gap≥0）
            abs_gap = current_score - next_score
            # 计算相对差距：绝对差距 / 当前文档分数（+1e-6避免除数为0/极小值，防止程序报错）
            # 1e-6 是 Python 中科学计数法的写法，等价于 0.000001（10 的负 6 次方，也就是百万分之一）。
            rel_gap = abs_gap / (abs(current_score) + 1e-6)

            # 触发断崖截断条件：绝对差距≥绝对阈值 OR 相对差距≥相对阈值
            # 满足任一条件，说明下一条文档相关性骤降，截断在当前位置
            if abs_gap >= RERANK_GAP_ABS or rel_gap >= RERANK_GAP_RATIO:
                # 最终取前i+1条（索引转实际数量，如i=2 → 取前3条）
                cutoff_pos = idx + 1
                logger.debug(f"断崖检测: 位置 {idx + 1}, abs_gap={abs_gap:.4f}, rel_gap={rel_gap:.4f}")
                break

        return ranked_docs[:cutoff_pos]

    def _merge_multi_source_docs(self, state: QueryGraphState) -> List[Dict[str, Any]]:
        """合并本地 RRF 结果和网络搜索结果为统一格式"""

        final_docs = []

        # 1. 获取本地 RRF 的文档
        for rrf_doc in (state.get('rrf_chunks') or []):

            if not isinstance(rrf_doc, dict):
                continue

            format_rrf_doc = {
                "content": rrf_doc.get('content'),
                "title": rrf_doc.get('title'),
                "chunk_id": rrf_doc.get('chunk_id'),
                "item_name": rrf_doc.get('item_name'),
                "url": "",
                "source": "local"
            }
            final_docs.append(format_rrf_doc)

        # 2. 获取 web 远程的文档
        for web_doc in (state.get('web_search_docs') or []):
            if not isinstance(web_doc, dict):
                continue

            format_web_doc = {
                "content": web_doc.get('snippet'),
                "title": web_doc.get('title'),
                "chunk_id": None,
                "url": web_doc.get('url'),
                "source": "web"
            }
            final_docs.append(format_web_doc)

        logger.info(f"收集到准备进行 Rerank 精排的文档 {len(final_docs)}")

        return final_docs

    def _rerank_merged_docs(self, user_query: str, merged_multi_docs: List[Dict[str, Any]], passages: bool = False) -> List[Dict[str, Any]]:
        """使用 Reranker 模型对文档进行精排"""
        if not merged_multi_docs:
            return []

        try:

            from app.evidence.manual_retrieval import score_original_documents, score_document_passages
            scorer = score_document_passages if passages else score_original_documents
            return scorer(user_query, merged_multi_docs, rerank_documents)

        except Exception as e:
            self.last_error = type(e).__name__
            # 百炼重排序不可用时，按前面 RRF 的顺序继续，避免整次问答失败
            logger.error(f"Rerank 重排序失败，改用融合排序结果：{str(e)}")
            return [
                {**doc, "score": float(len(merged_multi_docs) - index)}
                for index, doc in enumerate(merged_multi_docs)
            ]

if __name__ == "__main__":

    logger.info("开始测试: 重排序节点 (RerankNode)")

    mock_state = {
        "rewritten_query": "怎么测这块主板的短路问题？",
        "rrf_chunks": [
            {
                "chunk_id": "local_1",
                "title": "主板维修手册",
                "content": "主板短路通常表现为通电后风扇转一下就停，可以使用万用表的蜂鸣档测量。"},
            {
                "chunk_id": "local_2",
                "title": "闲聊",
                "content": "今天中午去吃猪脚饭吧，这块主板外观很漂亮。"},
        ],
        "web_search_docs": [
            {
                "url": "https://example.com/repair",
                "title": "短路查修指南",
                "snippet": "主板通电前先打各主供电电感对地阻值，阻值偏低就是短路。"},
            {
                "url": "https://example.com/news",
                "title": "科技新闻",
                "snippet": "苹果发布新款手机，A系列芯片性能提升20%。"},
        ],
    }

    logger.info("【输入状态】:")
    logger.info(f"  查询: {mock_state['rewritten_query']}")
    logger.info(f"  本地文档: {len(mock_state['rrf_chunks'])} 篇")
    logger.info(f"  网络文档: {len(mock_state['web_search_docs'])} 篇")

    node_rerank = NodeRerank()
    result = node_rerank(mock_state)

    logger.info("【重排序结果】:")
    for i, doc in enumerate(result["reranked_docs"], 1):
        score = doc.get('score')
        score_str = f"{score:.4f}" if score is not None else "N/A"
        logger.info(f"[{i}] score={score_str} | {doc['source']:5} | {doc['content'][:50]}...")

    logger.info("测试完成")
