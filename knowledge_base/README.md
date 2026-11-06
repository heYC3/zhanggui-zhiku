# 掌柜智库运行目录

从这里启动。完整说明在上一级的 [README.md](../README.md)。

只跑 Listing 时，页面默认账号是「小明」，密码是 `demo-pass`（仅本地演示，公开后请改掉）。

PowerShell：

```powershell
$env:LISTING_ACCOUNTS = "小明:demo-pass:editor:org-demo"
pip install -r requirements-listing.txt
python -m unittest discover -s test/listing -v
python -m app.listing_agent
```

bash / Linux（可选，和上面等价）：

```bash
export LISTING_ACCOUNTS='小明:demo-pass:editor:org-demo'
pip install -r requirements-listing.txt
python -m unittest discover -s test/listing -v
python -m app.listing_agent
```

打开 http://127.0.0.1:8002/listing.html ，用「小明」和 `demo-pass` 登录，再生成草稿。

完整 RAG：复制 `.env.example` 为 `.env` 并填写，再按上级 README 安装 `requirements-rag.txt`，然后 `python -m app.server`。不要提交填好的 `.env`。

- 导入页面：http://127.0.0.1:8000/import.html
- 问答页面：http://127.0.0.1:8001/chat.html
- Listing 页面：http://127.0.0.1:8002/listing.html
- 演示文稿：`data/samples/示例-掌柜耳机说明书.md`

数据库编排在 `deploy/docker-compose.yml`。卷持久由 GitHub Actions 的 volume 任务验证，本机可选 Docker。
