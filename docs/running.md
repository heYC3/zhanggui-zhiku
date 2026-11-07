# 运行指南

[返回首页](../README.md) · [架构与实现](architecture.md) · [评测说明](evaluation.md)

使用Python 3.11。以下命令在仓库根目录开始；创建虚拟环境后，所有Python命令使用该环境。

## Listing本地演示

不需要向量库或模型密钥，使用自编商品、离线证据与原句草稿。

PowerShell：

```powershell
cd knowledge_base
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-listing.txt
$env:LISTING_ACCOUNTS = "小明:demo-pass:editor:org-demo"
python -m app.listing_agent
```

Linux / macOS：

```bash
cd knowledge_base
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-listing.txt
export LISTING_ACCOUNTS='小明:demo-pass:editor:org-demo'
python -m app.listing_agent
```

打开 <http://127.0.0.1:8002/listing.html>，使用上述演示账号。输入明确商品型号，检查证据与草稿，确认后查看写入回执。测试相近型号时可分别使用X1和X1 Pro。

服务仅绑定本机。演示账号不能用于公网部署；服务端账号、角色和机构由`LISTING_ACCOUNTS`配置。实际数据默认落在已忽略的`listing_data/runtime/`。

## Shopify 测试店

Shopify 可选模式的配置、持久化目录、仅建 DRAFT 约束和真实验收证据见[Shopify 接入](shopify.md)。默认本地演示仍写本地台账。

## 完整手册问答

在`knowledge_base`中复制`.env.example`为`.env`，填写自己的密钥和数据库地址。PowerShell使用`Copy-Item .env.example .env`，bash使用`cp .env.example .env`。

```powershell
# 按需要启动Milvus、MongoDB与可选MinIO
# 需要已安装并启动Docker；本轮未在本机构建完整RAG容器

docker compose -f deploy/docker-compose.yml up -d
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-rag.txt
python -m app.server
```

| 页面 | 本机地址 |
| --- | --- |
| 手册导入 | <http://127.0.0.1:8000/import.html> |
| 手册问答 | <http://127.0.0.1:8001/chat.html> |
| Listing | <http://127.0.0.1:8002/listing.html> |

可上传仓库内[自编Markdown示例](../knowledge_base/data/samples/示例-掌柜耳机说明书.md)，再询问其型号和续航。它不经过PDF解析；PDF需另配MinerU令牌，图片摘要需可用的视觉模型。

| 配置 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 问答与看图模型的接口 |
| `LLM_DEFAULT_MODEL` / `VL_MODEL` | 文本和视觉模型，需与自己的接口兼容 |
| `MINERU_API_TOKEN` | PDF转Markdown |
| `BGE_M3_PATH` / `BGE_DEVICE` / `BGE_FP16` | 本地BGE缓存、设备、精度；CPU可用`cpu`和`0` |
| `MILVUS_URL` | 向量库，默认连接本机服务 |
| `MANUAL_IMAGE_BACKEND` | 默认`local`，图片由导入与问答服务提供；`minio`需另行验证 |
| `MANUAL_ASSET_DIR` | 留空使用`output/manual-assets`；内容哈希图片只保留本机 |
| `CHAT_HISTORY_ENABLE` / `MONGO_URL` | 聊天历史开关与MongoDB地址 |
| `WEB_SEARCH_ENABLE` | 联网搜索开关；复核手册证据时建议`false` |
| `DASHSCOPE_API_KEY` / `TEXT_RERANK_MODEL` | 百炼检索重排与可选联网搜索 |
| `LISTING_DRAFT_MODE` | 默认`offline`；`llm`才启用真实模型草稿 |
| `LISTING_LLM_PROVIDER` | 默认百炼，失败尝试OpenAI，再失败回退原句 |
| `LISTING_CHECKPOINT` | 默认`sqlite`；`file`仅用于文件快照演示 |

`.env.example`保留配置样例。BGE首次编码会加载或下载模型；服务页面可启动不等于PDF导入、向量库或真实模型已经验证成功。不要提交填好的`.env`、模型缓存、手册或运行数据库。

### 仅使用公开样例进行模型问答

已安装上面的 RAG 依赖、配置自己的文本模型后，可在 `knowledge_base` 中启动公开样例问答。它使用仓库商品 JSON，不需要先导入 Milvus，也不验证远程手册路径：

```powershell
$env:MANUAL_CATALOG_PATH = Join-Path (Get-Location) 'data/samples/demo_catalog.json'
$env:EVIDENCE_BACKEND = 'offline'
$env:CHAT_HISTORY_ENABLE = 'false'
$env:WEB_SEARCH_ENABLE = 'false'
python -m uvicorn app.query_process.api.query_service:app --host 127.0.0.1 --port 8001
```

打开 <http://127.0.0.1:8001/chat.html>，询问“掌柜示例耳机 X1 续航多久？这个时间包括通话吗？”。目录以 SKU 作为规范 `item_name`，自然语言名称作为别名，与公开证据的产品标识一致；只登记三个自编商品，事实仍从 `listing_data/products/` 读取。模型请求会使用自己的接口额度。截图与本轮远程导入失败记录见[实操图集](gallery.md)。

切换到完整手册服务前，在新终端恢复所需配置，清除 `MANUAL_CATALOG_PATH` 与 `EVIDENCE_BACKEND`；公开样例目录不包含私有手册名称。

## 测试与公开基线

在`knowledge_base`中执行：

```powershell
python -m pip install -r requirements-listing.txt -r requirements-rag-tests.txt
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LISTING_SHOP_BACKEND = 'local'
python -m unittest discover -s test/listing -v
python -m unittest discover -s test/rag -v
python -m app.listing_agent.eval
```

合成测试不需要PyTorch、Milvus、真实手册或模型密钥。本地若已有`.env`，运行测试时设置`PYTHON_DOTENV_DISABLED=1`，避免读取本机配置。公开基线重算只适用于Listing；私有手册首次验收不在这里重跑。

## Listing接口与MCP

| 接口 | 用途 |
| --- | --- |
| `POST /api/listing/sessions` | 登录获取Bearer令牌 |
| `POST /api/listing/tasks` | 生成任务；身份由令牌确定 |
| `GET /api/listing/tasks/{task_id}` | 查看任务、证据、草稿与状态 |
| `GET /api/listing/stream/{task_id}` | SSE进度 |
| `POST /api/listing/tasks/{task_id}/confirm` | 携带草稿版本确认写入 |
| `POST /api/listing/tasks/{task_id}/reject` | 拒绝草稿 |
| `POST /api/listing/tasks/{task_id}/cancel` | 取消任务 |
| `POST /api/listing/tasks/{task_id}/reconcile` | 核对结果不明的写入 |

规则与证据工具可通过MCP stdio启动：

```powershell
python -m app.listing_agent.mcp_server
# 另开终端运行官方客户端探针；记录会覆盖本地同名公开报告文件
python -m app.listing_agent.mcp_probe
```

真实模型与模板对照使用`python -m app.listing_agent.llm_compare`，会调用已配置接口并更新对照报告，不属于默认无密钥检查。工具调用本身不写台账。

## 可选Listing容器

```bash
docker compose -f deploy/docker-compose.listing.yml up --build
```

容器定义只覆盖Listing；同卷两次启动由CI的volume任务检查，完整RAG部署不能据此宣称已验收。
