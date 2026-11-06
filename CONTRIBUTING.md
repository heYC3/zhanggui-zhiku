# 参与货知

## 跑 Listing 测试

在 `knowledge_base` 目录安装依赖后执行：

```bash
pip install -r requirements-listing.txt
python -m unittest discover -s test/listing -v
```

PowerShell 使用同一条 `python` 命令。页面登录和启动方式见仓库根目录 [README.md](README.md)。

## 不要提交密钥

不要提交 `.env`、令牌、密码或模型权重。配置只改 `.env.example` 里的占位说明。本地演示账号写在 README，公开环境请另行更换。

## 拉取请求

PR 保持小，并且可以在本地测完。改动 Listing 时，先跑上面的 `unittest discover`。
