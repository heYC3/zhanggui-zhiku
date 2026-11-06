# 掌柜智库运行目录

从这里启动。完整说明在上一级的 [README.md](../README.md)。

只跑 Listing：

```powershell
pip install -r requirements-listing.txt
python -m unittest discover -s test/listing -v
python -m app.listing_agent
```

完整 RAG：复制 `.env.example` 为 `.env` 并填写，再按上级 README 安装 `requirements-rag.txt`，然后 `python -m app.server`。

- 导入页面：http://127.0.0.1:8000/import.html
- 问答页面：http://127.0.0.1:8001/chat.html
- Listing 页面：http://127.0.0.1:8002/listing.html
- 演示文稿：`data/samples/示例-掌柜耳机说明书.md`

数据库编排在 `deploy/docker-compose.yml`。
