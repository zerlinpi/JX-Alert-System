# JX Alert System

领星 ERP（Lingxing OpenAPI）只读数据监控、销量/广告异常检测、飞书群自定义机器人通知与 MCP 查询服务。

> 当前阶段：第二轮基础集成。默认 `APP_DRY_RUN=true`、`FEISHU_ENABLED=false`、`SCHEDULER_ENABLED=false`。不提交真实密钥、不自动读取真实店铺、不自动向飞书群发消息。

## 当前架构

- **领星：** AppID / AppSecret 用于按账户文档配置 Token 获取和续约；业务 API 使用 Bearer Token + URL Query 签名。Token 路径、请求体和响应字段路径必须从当前领星开放平台文档确认后填写。
- **飞书：** 使用群自定义机器人 Webhook 单向推送到一个指定群，不使用飞书应用机器人或 tenant_access_token。
- **数据库：** MariaDB 11；本地测试支持 SQLite。保留原始快照、日指标、异常事件、通知日志和 outbox。
- **异常：** 订单量相对前七个完整自然日均值变化默认 ±15%；广告高花费、达到花费/点击门槛但无转化、低花费无转化和预算消耗提示。
- **可靠通知：** 幂等 outbox、worker claim、失败退避重试和投递日志。Webhook 本身不支持外部幂等键，因此进程在“飞书已接收、数据库尚未标记成功”之间崩溃时，重试可能产生重复消息（at-least-once）。
- **Excel：** 每日多工作表导出模块已建立。
- **MCP：** 提供健康状态和已存储异常查询；尚未开放真实采集触发工具。

## 已核验的领星业务 API

| 功能 | 方法 / 路径 |
|---|---|
| 店铺列表 | GET `/erp/sc/data/seller/lists` |
| MSKU/ASIN 每日销量统计 | POST `/erp/sc/data/sales_report/asinDailyLists` |
| SP 活动日报 | POST `/pb/openapi/newad/spCampaignReports` |
| SB 活动日报 | POST `/pb/openapi/newad/hsaCampaignReports` |
| SD 活动日报 | POST `/pb/openapi/newad/sdCampaignReports` |

广告接口使用 `X-API-VERSION: 2`。领星签名按 ASCII 排序，将业务参数、原始 `access_token`、`app_key`、`timestamp` 拼接后 MD5 大写，再使用 AppID 作为 AES 密钥执行 AES/ECB/PKCS5Padding。Bearer Token 通过 `Authorization` 头发送，但仍须参与签名；POST JSON 中的标量业务字段也须参与签名。当前客户端对嵌套对象/数组签名会 fail closed，不会猜测序列化规则。

官方资料：
- [领星 OpenAPI 接入指南](https://apidoc.lingxing.com/new/guides/new-instructions/)
- [领星开放平台](https://apidoc.lingxing.com/new)
- [店铺列表](https://apidoc.lingxing.com/new/api-docs/BasicData/SellerLists/)
- [MSKU/ASIN 销量统计](https://apidoc.lingxing.com/new/api-docs/Statistics/AsinDailyLists/)
- [SP 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/spCampaignReports/)
- [SB 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/hsaCampaignReports/)
- [SD 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/sdCampaignReports/)

## 飞书自定义机器人

自定义机器人只向创建它的群发送消息。Webhook 地址和签名密钥都属于敏感凭据，必须只保存在 Linux 服务器的 `.env` 中。

- 文本消息支持 `<at user_id="OPEN_ID">姓名</at>` 实现真正 @。
- 群自定义机器人无法通过手机号/邮箱查询用户 ID；请在路由配置中使用该群成员的真实 Open ID。
- `FEISHU_KEYWORD` 用于满足机器人安全设置中的自定义关键词校验（若启用）。
- `FEISHU_SIGN_SECRET` 用于签名校验（若启用）。
- `config/feishu-routing.example.json` 可复制为服务器本地 `data/feishu-routing.json`，按店铺 ID 和 MSKU/ASIN 配置负责人 Open ID。
- 一个 Webhook 对应一个群；当前配置面向一个主告警群。

官方文档：[飞书自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

## Linux Docker 部署

1. 在 Linux 服务器安装 Docker Engine 和 Docker Compose Plugin。
2. 克隆仓库并复制配置模板：
   ```bash
   git clone https://github.com/zerlinpi/JX-Alert-System.git
   cd JX-Alert-System
   cp .env.example .env
   ```
3. 生成独立数据库密码并编辑 `.env`。至少填写 `DB_PASSWORD`、`DB_ROOT_PASSWORD`、领星 AppID/AppSecret。不要把 `.env` 发到聊天或提交到 Git。
   ```bash
   openssl rand -hex 32
   nano .env
   ```
4. 创建可由容器非 root 用户写入的数据目录：
   ```bash
   mkdir -p data
   cp config/feishu-routing.example.json data/feishu-routing.json
   sudo chown -R 10001:10001 data
   chmod 700 data
   chmod 600 data/feishu-routing.json
   ```
5. 先保持 dry-run，验证 Compose 配置并启动 API 与数据库：
   ```bash
   docker compose config
   docker compose up -d --build
   docker compose ps
   curl http://127.0.0.1:8000/health
   ```
6. 阅读 [Linux 部署说明](docs/DEPLOY_LINUX.md)、[领星 Token 配置说明](docs/LINGXING_TOKEN_SETUP.md) 和 [飞书 Webhook 配置说明](docs/FEISHU_WEBHOOK.md)。

调度器是可选 profile。只有完成 API 契约、历史回补、飞书测试后才配置 `SCHEDULER_ENABLED=true`，再执行：
```bash
docker compose --profile scheduler up -d --build scheduler
```

默认 API 仅绑定 `127.0.0.1`，建议通过服务器 Nginx/Caddy 反向代理访问，不要直接暴露到公网。生产环境请另行配置 TLS、访问控制和防火墙。

## 测试

CI 会在 push / pull request 时安装开发依赖并运行 pytest。服务器上可在隔离环境运行：
```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

飞书连通性脚本默认只做本地配置检查，不发送消息。只有明确准备好向真实群发送测试时才加 `--send`：
```bash
python scripts/test_feishu_webhook.py
python scripts/test_feishu_webhook.py --send
```

## 尚未达到生产完成的部分

- 领星 Token 接口的准确路径、请求字段和响应字段路径尚需按你账号开放平台当前文档填写并验证。
- 领星销量/广告响应 JSON 的标准化适配、游标分页采集、历史回补和每日完整数据流水线尚未完成。
- 绩效通知接口尚未确认。
- 店铺/站点时区、币种、负责人映射需由真实账号数据确认。
- 当前数据库初始化使用 SQLAlchemy `create_all`；尚未加入 Alembic 版本迁移。
- 尚未在你的 Linux 服务器执行 Docker 部署，也未进行真实领星或飞书端到端测试。

第一阶段只读领星，不提供修改广告预算、竞价、关键词或活动状态的功能。
