# 掌柜智库

基于 LangGraph 的商品知识 RAG 与 Listing Agent：将商品资料转成可追溯证据，通过有状态工作流完成多轮问答与受控商品草稿执行。

[![CI](https://github.com/heYC3/zhanggui-zhiku/actions/workflows/listing-ci.yml/badge.svg)](https://github.com/heYC3/zhanggui-zhiku/actions/workflows/listing-ci.yml)
![Python](https://img.shields.io/badge/Python-3.11-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

商品参数分散在 PDF、表格和图片中，相近型号容易串用，多轮追问又常常省略商品名；生成文案时，还需要约束资料中没有的卖点。项目围绕这些问题实现**知识处理 → 型号与上下文理解 → 混合检索 → 有依据的生成 → 校验与人工确认**，让问答和 Listing 复用同一证据能力。

技术栈：**Python · LangGraph · LangChain · FastAPI · MinerU · BGE-M3 · Milvus · MongoDB · SQLite · SSE · MCP**。

[架构与设计](docs/architecture.md) · [实操图集](docs/gallery.md) · [评测依据](docs/evaluation.md) · [运行指南](docs/running.md)

## 核心结果

以下为截至 2026-11-08 的已有验证记录。本次文档调整未新增实验，各任务分别计分。

| 能力 | 量化结果 | 验证范围 |
| --- | --- | --- |
| 问答质量 | 严格整题通过率 **95.8%（23/24）** | 真实手册的 24 道首次新题，逐题核对事实、主体、行为及必要图片 |
| 证据检索 | 正文 **hit@3 100%（20/20）**；实际上下文指定要点 **47/47** | 新题中有明确正文事实的 20 题；命中与要点覆盖分别计分 |
| 型号与澄清 | 产品路由 **24/24**；资料不足与澄清行为 **4/4** | 同一批私有新题，不外推一般场景准确率 |
| Listing 闭环 | **68.8%（11/16）→100%（16/16）** | 关键词基线与当前综合流程在 16 道公开合成草稿 / 流程题上的对照 |
| 工程回归 | **111 项通过（72 Listing＋39 RAG）** | 合成回归覆盖型号边界、检索契约、鉴权、确认、并发、幂等与恢复；CI 另查基线及容器卷持久 |

知识处理验收覆盖 **5 本手册、298 条正文**；另完成 **413 张去重图片**的 HTTP/MIME 校验。严格整题仍有 1 题显式选图失败（选图 4/5）；hit@3 只要求前三条中至少命中一条相关证据。

私有新题在固定系统后运行一次，由同一开发者借助 Codex 出题和复核，非独立盲评；Listing 对照使用离线自编数据、字块稠密替身与模板 HyDE，多个组件共同变化，不作为单算法增益或商家运营收益。完整分母、来源摘要及失败记录见[评测说明](docs/evaluation.md)与[公开基线](knowledge_base/listing_data/reports/baseline.md)。

## 系统架构

![三条工作流与共享证据接口](docs/assets/architecture.svg)

导入工作流建设知识，问答工作流理解问题并组织回答，Listing 工作流将证据用于可审核的草稿与业务动作。三条流程保留各自的状态和生成目标，问答与 Listing 通过共享证据接口复用资料与检索能力。

系统采用预定义 LangGraph 状态图编排模型与工具。流程节点、分支、并行召回和确认门由应用明确控制；设计细节与可选后端见[架构与实现](docs/architecture.md)。

## 核心实现

| 方向 | 问题与设计 | 代码入口 |
| --- | --- | --- |
| 知识处理与证据建库 | 将 PDF / Markdown、图片和表格组织为带来源的证据：MinerU 解析、视觉摘要、按标题切片、BGE-M3 稠密 / 稀疏编码；保留产品、正文与来源信息 | [导入工作流](knowledge_base/app/import_process/agent/main_graph.py) · [共享证据](knowledge_base/app/evidence/store.py) |
| 状态与流程编排 | 为导入、问答、Listing 分别定义状态与路由；导入按文件类型分流，问答在澄清后结束或并行检索，Listing 在人工确认处暂停并通过检查点恢复 | [问答图](knowledge_base/app/query_process/agent/kb_query_workflow.py) · [Listing 图](knowledge_base/app/listing_agent/workflow.py) |
| 多轮记忆与问题理解 | 通过 MongoDB 历史和调用方显式历史处理省略与指代；先用目录和别名确定型号边界，再结合历史改写为完整问题，歧义或未知型号进入澄清 | [主体与改写](knowledge_base/app/query_process/agent/nodes/node_item_name_confirm.py) · [型号解析](knowledge_base/app/evidence/identity.py) |
| 混合检索与上下文 | 在产品范围内执行原问题 / HyDE 召回，经 RRF、模型精排及表格行窗口处理组织证据；跨型号候选保留各自配额，记录实际生成上下文，图片只从上下文取源 | [手册检索](knowledge_base/app/evidence/manual_retrieval.py) · [重排](knowledge_base/app/query_process/agent/nodes/node_rerank.py) |
| 草稿与受控执行 | 复用证据生成结构化草稿，通过 Schema、业务规则与人工确认约束动作；服务端检查身份、归属和草稿版本，以幂等键、SQLite 检查点和结果核对控制重复写入 | [草稿生成](knowledge_base/app/listing_agent/llm_draft.py) · [执行服务](knowledge_base/app/listing_agent/service.py) |
| 服务接口与工具 | FastAPI / SSE 分离任务执行、状态查询和流式展示；MCP 提供查证据与校验草稿工具，回归与故障记录覆盖服务和执行边界 | [问答 API](knowledge_base/app/query_process/api/query_service.py) · [Listing API](knowledge_base/app/listing_agent/api.py) · [MCP](knowledge_base/app/listing_agent/mcp_server.py) |

这是个人工程项目，使用开源组件并借助 Codex 辅助开发与核验。重点工作包括知识处理、工作流编排、多轮理解、共享证据与检索、受控草稿及恢复，以及评测和故障归因。代码与自编演示数据按 [MIT](LICENSE) 发布，第三方手册不随仓库分发。

## 实操展示

**问答：完整型号提问，保留资料中的使用限制。**

![自编 X1 资料的真实模型问答](docs/assets/chat_demo_2026-11-08.jpg)

该图使用公开自编 X1 资料、离线证据后端与真实模型，关闭联网；展示证据到回答的实际页面，不计入私有手册验收。2026-11-08 补图时远程 Milvus 导入连接失败，失败页面与排障说明保留在[实操图集](docs/gallery.md)。

**Listing：查看草稿与原始证据，再由人工确认。**

![Listing 草稿与证据，等待人工确认](docs/listing/draft.png)

[查看确认后写入台账的画面](docs/listing/written.png)。默认公开样例使用自编商品、离线证据与原句草稿，无需模型密钥；可选真实模型草稿路径。两种模式的记录分别保留。

[完整实操图集](docs/gallery.md)还包含导入排障、草稿与回执、评测摘要、MCP 调用和 CI 页面，每张图注明来源与验证范围。

## 外部系统接入与部署

在知识与执行基座之上，已完成以下外部集成验证：

| 扩展 | 已验证结果 | 证据 |
| --- | --- | --- |
| Shopify 开发店 | 人工确认后创建真实 DRAFT，独立读回；重复确认与服务重启后恢复同一回执，未知结果先核对 | [适配器](knowledge_base/app/listing_agent/shopify_adapter.py) · [接入验收](docs/shopify.md) · [脱敏回执](docs/listing/shopify_receipt_2026-11-08.json) |
| 单账号云端 Listing | PythonAnywhere 外部登录、真实 P20 DRAFT、平台重载、停服备份与空目录恢复；重复确认未新增商品 | [部署验收](docs/pythonanywhere.md) · [备份恢复](knowledge_base/app/listing_agent/backup.py) |

![Shopify 开发店真实 DRAFT 与回执](docs/listing/shopify_receipt_preview_2026-11-08.png)

**在线入口：** [Listing 测试版](https://zhanggui.pythonanywhere.com/)（需授权账号，仅本人测试）。云端部署 Listing 与公开自编商品；手册导入和完整 RAG 在本机运行。真实店仅验证正常创建、重复确认与成功任务恢复，故障窗口另由离线模拟覆盖。

## 快速运行

Python 3.11，在仓库根目录执行（PowerShell）：

```powershell
cd knowledge_base
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-listing.txt
$env:LISTING_ACCOUNTS = "小明:demo-pass:editor:org-demo"
python -m app.listing_agent
```

打开 <http://127.0.0.1:8002/listing.html>。上面的账号仅用于本地演示；登录后选择示例商品，生成草稿，再确认写入。Linux / macOS、完整手册问答及公开样例模型问答的配置见[运行指南](docs/running.md)。

无密钥回归：

```powershell
python -m pip install -r requirements-rag-tests.txt
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LISTING_SHOP_BACKEND = 'local'
python -m unittest discover -s test/listing -v
python -m unittest discover -s test/rag -v
```

CI 检查 Listing 基线、两组单元回归及 Listing 容器卷持久，不调用私有手册、付费模型或真实店铺。[实时 Actions](https://github.com/heYC3/zhanggui-zhiku/actions/workflows/listing-ci.yml)以对应提交为准。

## 项目目录

```text
knowledge_base/
├── app/import_process/  PDF / Markdown、图片与知识建库工作流
├── app/query_process/   多轮理解、混合检索与流式问答
├── app/evidence/        共享证据、型号识别、手册检索与图片
├── app/clients/         Milvus、MongoDB 历史与可选 MinIO
├── app/lm/              文本 / 视觉模型与向量编码
├── app/listing_agent/   草稿、校验、确认、恢复与 MCP 工具
├── listing_data/        自编商品、公开合成测集与结果
├── test/rag/            合成 RAG 回归
├── test/listing/        Listing 与恢复回归
└── deploy/             服务编排与 Listing 镜像定义
```

- [架构与实现](docs/architecture.md)：状态编排、多轮记忆、证据检索与执行恢复。
- [评测说明](docs/evaluation.md)：分母、基线、首次验收和已知失败。
- [运行指南](docs/running.md)：本地启动、配置、测试与接口。
- [发布自检](docs/self-audit-2026-11-08.md)：v0.2.0 发布时的验收与展示记录。

## 当前范围

指定 Shopify 开发店只创建 DRAFT，在线测试版为单账号 Listing。多店商家授权、真实店故障注入、生产规模多租户与分布式高可用尚未验收。图片验证覆盖本机 HTTP/MIME 和真实流式浏览器显示，MinIO 后端未完成验收。

私有手册历史验收与最近公开导入尝试分别留存：公开样例的远程 Milvus 连接失败，不能把离线证据问答图拼成一次成功的远程导入问答链路。手册、私有题、原图、模型、数据库和凭据均不随仓库发布。

[参与贡献](CONTRIBUTING.md) · [安全问题](SECURITY.md) · [MIT 许可](LICENSE)