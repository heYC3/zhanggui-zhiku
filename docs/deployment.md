# 第三步：公开 Listing 在线测试版

当前状态：PythonAnywhere 免费在线测试版已完成本轮验收，仅本人登录。公网入口、实验平台限制、真实草稿回执、重载与备份恢复证据见 [PythonAnywhere 部署记录](pythonanywhere.md)。

用户要求不付费、不绑银行卡且关闭本机后仍可访问。本次使用已注册的 PythonAnywhere Beginner，没有创建 Oracle 或其他收费资源。以下 Docker/Caddy 云主机配置作为备用方案保留，尚未实际部署，不应将其命令视为本次在线环境。

## 备用容器方案的部署边界

仅部署 Listing 和三个自编 SKU；私有手册、Milvus、私有评测不打包。镜像只复制商品、规则和模型说明卡；`.dockerignore` 排除手册、运行数据、密钥与评测目录。首版固定原句草稿，不需要上传模型 Key。

单实例、SQLite、完整运行目录 `/var/data/runtime`。启动检查要求持久盘挂载在 `/var/data`，并固定店铺 `zhanggui-test.myshopify.com`、Shopify 后端、SQLite 检查点、原句草稿、关闭故障注入和 `.env` 自动加载。首版只配置一个编辑账号，随机密码至少 20 位；启动时移除旧账号并在改密后撤销旧会话。

网页需登录才可访问，登录 Cookie 为 HttpOnly / Secure / SameSite=Strict，12 小时到期；保留原有 Bearer API。退出会撤销令牌。跨来源写请求被拒绝。线上看板只统计当前账号的在线任务，重复状态按任务去重，供应商多次尝试合并，缺 token 或费用保持未知，不读取离线对照日志。

## Docker/Caddy 备用方案（未部署）

以下命令在新建 Ubuntu 实例执行。已有服务器先检查环境和端口，避免覆盖现有服务。

1. 安装 Docker Engine 与 Compose 插件，按 [Docker Ubuntu 官方文档](https://docs.docker.com/engine/install/ubuntu/) 操作。Dockerfile 使用多架构 Python 基础镜像；本机未安装 Docker，amd64 / arm64 的实际构建与 Linux 卷挂载检查留给 CI 和服务器。
2. 克隆含本次变更的代码到 `/opt/zhanggui/repo`，或上传本机私有交付目录里的公开部署 ZIP 并解压到该目录，然后创建私有目录：

```bash
sudo install -d -m 700 /opt/zhanggui/data /opt/zhanggui/backups
# secrets.env 从本机私有交付文件安全上传，不要在命令行粘贴 Token。
sudo chmod 600 /opt/zhanggui/secrets.env
cd /opt/zhanggui/repo/knowledge_base
```

3. 私有配置格式见 [`online.env.example`](../knowledge_base/deploy/online.env.example)。填写域名、现有测试店 Token、全新随机账号密码。不要覆盖项目原有 `knowledge_base/.env`。无自有域名可试 `公网IP.sslip.io`，它依赖第三方免费 DNS 和证书签发，尚未实测。
4. 云安全组及系统防火墙只开放 80/443；SSH 22 限制本人来源 IP。应用 8002 只映射到服务器 `127.0.0.1`，不在云安全组开放。Caddy 自动申请 HTTPS 证书；DNS 必须先指向实例公网 IP。

```bash
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml up -d --build
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml ps
```

日志默认不打印密钥。不要直接分享展开后的 `docker compose config`、容器完整 inspect 或环境输出，它们可能包含 Token。服务进程锁和备份进程锁都在持久盘内，拒绝第二实例或服务运行中的维护。

## 停服备份与恢复

备份包含鉴权、台账、写入意图、回执、会话、事件、LangGraph 检查点及在线日志，属于私有文件；SHA-256 清单只检测损坏，不证明来源可信。仅恢复自己保存的可信备份，保留一份服务器外的副本。

在 `knowledge_base` 目录执行，下面使用固定示例文件名，每次备份应换新名字；工具拒绝覆盖已有备份：

```bash
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml stop listing-agent
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml run --rm --no-deps listing-agent python -m app.listing_agent.backup backup /backups/first.zip
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml start listing-agent
```

恢复只接受不存在或空的运行目录；禁止覆盖。先停服并把现有运行目录改名保留，再恢复：

```bash
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml stop listing-agent
# 先确认 /opt/zhanggui/data/runtime 确实是本服务目录，并确认备份文件存在。
sudo mv /opt/zhanggui/data/runtime /opt/zhanggui/data/runtime.before-restore
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml run --rm --no-deps listing-agent python -m app.listing_agent.backup restore /backups/first.zip
sudo docker compose --env-file /opt/zhanggui/secrets.env -f deploy/docker-compose.online.yml start listing-agent
```

启动后核对原任务、检查点与回执，重复确认同一任务不新增商品；备份失败时先恢复原目录，不以空库继续写店铺。不要用 `docker compose down -v` 清理持久卷。

## 本轮完成范围

PythonAnywhere 的外部登录、一次真实 DRAFT 写入、独立读回、重复确认、重载持久化、停服备份与空目录恢复均已验证。完整结果和边界以 [PythonAnywhere 记录](pythonanywhere.md)为准。Docker 镜像与 HTTP/卷检查以当前提交的 CI 结果为准；多架构构建、云主机防火墙及自有域名 Caddy 证书未实测。第三步是单账号在线测试版，不代表生产部署、多租户或高可用。
