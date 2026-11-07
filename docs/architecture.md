# 架构与实现

[返回首页](../README.md) · [运行指南](running.md) · [评测说明](evaluation.md)

## 三条工作流

| 流程 | 输入与输出 | 实现入口 |
| --- | --- | --- |
| 导入 | PDF / Markdown → 带来源的正文与图片 → Milvus | [kb_import_app](../knowledge_base/app/import_process/agent/main_graph.py) |
| 问答 | 问题与显式历史 → 型号确认 → 证据检索 → 答案与图片 | [KBQueryWorkflow](../knowledge_base/app/query_process/agent/kb_query_workflow.py) |
| Listing | 商品要求 → 证据 → 草稿 / 缺口 → 校验 → 人工确认 → 本地写入 | [Listing workflow](../knowledge_base/app/listing_agent/workflow.py) |

[app/server.py](../knowledge_base/app/server.py) 启动三个本地服务，分别使用 8000、8001、8002 端口。问答和 Listing 保持不同状态与生成目标，通过 [evidence/store.py](../knowledge_base/app/evidence/store.py) 复用证据接口。演示商品使用离线资料；目录外手册使用 Milvus。两类后端的验证结果分别报告。

## 导入与证据

1. PDF 经 MinerU 转成 Markdown；原生文本可读的封面缺字由 [pdf_text.py](../knowledge_base/app/evidence/pdf_text.py) 补充，记录源 PDF 摘要、页码与编码异常。扫描页不冒充已恢复。
2. 图片保留原件，由视觉模型生成摘要。默认 [manual_assets.py](../knowledge_base/app/evidence/manual_assets.py) 发布内容哈希图片，地址为 `/manual-assets/<sha256>.<格式>`；MinIO 是可选后端。
3. 正文按标题和段落切片，识别产品后通过 BGE-M3 生成稠密 / 稀疏向量，写入 Milvus。
4. 证据接口携带 `product_id`、`chunk_id`、原文与来源。评测使用产品名和完整正文的稳定摘要，避免把数据库自增编号当成跨版本身份。

目录别名用于确认产品，不能代替正文证据。原 PDF 有、解析正文没有的内容归因到解析；召回、排序、上下文预算和生成问题分开检查。

## 问答检索

- **确认主体**：[identity.py](../knowledge_base/app/evidence/identity.py) 处理精确别名、相近型号与显式历史。缺型号或未知型号时澄清；跨型号题保留每个产品的候选。
- **召回**：原问题与 HyDE 分支进行向量检索；在确认产品范围内补充词语召回。HyDE 的猜测不加入词语检索。
- **融合与排序**：RRF 融合候选；[manual_retrieval.py](../knowledge_base/app/evidence/manual_retrieval.py) 将跨行 HTML 表格展开为可读文本，以完整正文及相关行窗口评分，并按产品交替交付。返回证据保留原文。
- **生成**：实际装入提示词的 `context_docs` 单独记录。答案引用的图片取自这些上下文，避免将未用于生成的候选算成依据。
- **失败**：向量库、编码与重排错误单列。MongoDB 聊天记录不可用时有超时和退避；BGE 采用懒加载，构造与预热由锁保护。

联网搜索是可选分支，私有手册验收关闭联网、隔离会话，只传明确历史。现有检索指标不覆盖开放网络问答质量。

## Listing 的受控执行

[workflow.py](../knowledge_base/app/listing_agent/workflow.py) 先确认商品，再取证据。`offline` 模式直接用原句；`llm` 模式让模型填充草稿字段，结构检查后进入规则校验，模型不可用时回退原句。

执行约束落在服务端：

| 约束 | 对应处理 |
| --- | --- |
| 身份与归属 | 服务端登记账号、Bearer 令牌、任务和机构归属检查 |
| 人工确认 | 草稿停在确认节点；查看身份不能确认，旧草稿版本不能写入 |
| 幂等写入 | 同一幂等键及同内容返回原任务；内容变化返回冲突 |
| 恢复 | SQLite 台账与 LangGraph SqliteSaver；确认前后强制结束进程的测试分开 |
| 结果不明 | 转入核对状态，不自动重复写入 |
| SSE | 进度事件持久化；订阅断开不等于取消业务任务 |

本地服务不是生产认证系统，写入的是本地台账。断电、跨机容灾和生产规模隔离尚无验证。

## MCP 与观测

[MCP server](../knowledge_base/app/listing_agent/mcp_server.py) 使用官方 Python SDK，提供查证据和校验草稿两个工具，不直接写台账。[客户端探针](../knowledge_base/app/listing_agent/mcp_probe.py) 覆盖初始化、工具列表、调用及参数错误，已有 [保存记录](../knowledge_base/listing_data/reports/mcp_client_record.json)。

真实模型对照记录模型、提示版本、token、耗时和失败类型；缺少用量或单价时费用记为未知。默认 trace 写本地 JSONL，配置 Langfuse 后才发送到外部服务。详见 [模型对照](../knowledge_base/listing_data/reports/llm_compare.md)。
