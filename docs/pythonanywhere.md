# PythonAnywhere 免费在线测试版

2026-11-08 已部署到 [zhanggui.pythonanywhere.com](https://zhanggui.pythonanywhere.com/)，使用 Beginner 免费账号、实验 ASGI、单 worker 和家目录 SQLite。账号邮箱已确认。仅限本人登录测试，密码单独私有交付；本次未开通付费资源，也未调用付费模型。

网站由云端运行，关闭本机不影响服务进程。需要在 PythonAnywhere 账号中定期续期；该账号页面显示周期为 3 个月，以账号实际到期时间为准。实验功能与免费额度可能变化，不能承诺永久免费或持续可用。参见[官方 ASGI 说明](https://help.pythonanywhere.com/pages/ASGICommandLine/)与[实验 API](https://help.pythonanywhere.com/pages/ASGIAPI/)。

## 本次真实验收

| 项目 | 已验证结果 |
| --- | --- |
| ASGI 探针 | 公网 HTTPS 成功；平台 reload 后 SQLite marker 不变、boot_count 从 1 变为 2 |
| Shopify 只读门 | 云端经官方代理读取固定测试店，抽样得到 2 个已有 DRAFT；该阶段没有 mutation |
| 完整工作台 | 外部浏览器登录、生成 P20 原句草稿并确认写入；首版仅本人一个编辑账号 |
| 真实写入 | 创建商品 `15465550020682`；mutation 后独立查询确认 DRAFT；完整操作标记只找到 1 件 |
| 重复与重载 | 同一任务重复确认、平台重载后重复确认，均保持同一回执与商品 |
| 停服恢复 | 暂停网站后备份 6 个运行文件，保留原目录，恢复至空目录；恢复后回执一致且重复确认不新增 |
| 备份副本 | 下载到本机，压缩包 SHA-256 与内部逐文件清单均校验通过；备份属于私有数据 |
| 访问控制 | 匿名页面 303 至登录页；匿名任务/看板、错误账号 401；跨来源及缺少来源的 Cookie 写请求 403；退出后会话 401 |
| 范围限制 | 公开模式拒绝真实模型模式，禁用 API 文档页面；未上传私有手册、评测、本机数据库或模型 Key |
| 看板 | 只有本次 1 个在线任务；原句模式没有模型调用，输入/输出 token 与费用均为 0 |

[脱敏回执](listing/pythonanywhere_receipt_2026-11-08.json)保留任务、商品、操作标记与独立查询证据。跨用户所有权测试由本地回归覆盖；云端只有一个授权账号，没有为了测试新增账号。

本地 Listing **72 项测试通过**。每次推送后的独立回归、公开基线与 Docker HTTP/卷检查以 [GitHub Actions 当前运行](https://github.com/heYC3/zhanggui-zhiku/actions/workflows/listing-ci.yml)为准，不沿用旧提交的 CI 结果。Docker/Caddy 备用方案没有实际部署到云主机，RAG 不在此次在线部署范围。

## 文件与启动

公开部署包按白名单包含 Listing、证据公共模块、3 个自编商品、演示平台规则、模型说明卡、轻量依赖与平台启动器。私有凭据不在包内。

| 用途 | 云端路径 |
| --- | --- |
| 程序 | `/home/zhanggui/zhanggui-listing/knowledge_base` |
| Python 3.11 虚拟环境 | `/home/zhanggui/.virtualenvs/zg-probe` |
| 私有配置 | `/home/zhanggui/.config/zhanggui/listing.env`，权限 600，父目录 700 |
| 运行数据 | `/home/zhanggui/.local/share/zhanggui/listing-runtime`，权限 700 |
| 私有备份 | `/home/zhanggui/.local/share/zhanggui/backups`，权限 700 |

私有配置只有 `LISTING_ACCOUNTS`、`SHOPIFY_STORE_DOMAIN`、`SHOPIFY_ADMIN_TOKEN`。账号操作 API Token 单独私有保存，未加载到 Listing 进程。

平台网站命令（`${DOMAIN_SOCKET}` 由平台展开）：

```bash
/home/zhanggui/.virtualenvs/zg-probe/bin/python /home/zhanggui/zhanggui-listing/knowledge_base/deploy/pythonanywhere/run_listing.py --uds ${DOMAIN_SOCKET}
```

启动器固定公开模式、`zhanggui-test.myshopify.com`、SQLite、原句草稿及单 worker；仅显式 PythonAnywhere 模式使用 `http://proxy.server:3128`，保留 TLS 验证和拒绝重定向。ASGI worker 不继承 Bash 控制台代理变量，启动器已显式设置官方代理；最初因此启动失败，修复后已通过真实写入与读回。

健康检查为 `/api/listing/health`。重载接口实际使用带尾斜杠的 `POST /api/v1/user/zhanggui/websites/zhanggui.pythonanywhere.com/reload/`，301 不视作成功。实验 API 目前只能 PATCH 网站启用状态，修改命令需要保留配置后删除并重建网站；本次只替换了临时探针定义，没有删除家目录文件。

## 日常维护

在平台账号内确认到期日期并及时续期。ASGI 网站可能不出现在常规 Web 标签页，应使用官方命令行或实验 API 管理。本次实际可用不代表未来套餐与接口不会改变。

备份前暂停网站并等待进程锁释放，运行：

```bash
cd /home/zhanggui/zhanggui-listing/knowledge_base
LISTING_RUNTIME_DIR=/home/zhanggui/.local/share/zhanggui/listing-runtime /home/zhanggui/.virtualenvs/zg-probe/bin/python -m app.listing_agent.backup backup /home/zhanggui/.local/share/zhanggui/backups/NEW-NAME.zip
```

每次使用新备份文件名。恢复前保留原运行目录，工具只接受空目录；恢复后重新启用并重载网站，核对原任务与回执。备份含鉴权与业务数据，不能提交公开仓，至少保留一份平台外副本。服务运行时工具拒绝维护；不要跳过进程锁。

本次记录的虚拟环境约 150 MB，尚在 512 MB 文件额度内；控制台 CPU 使用量是账号日配额，不是响应时间或可用性保证。准确剩余额度以账号页和 API 为准。
