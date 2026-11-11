# JX Alert System

领星 ERP（Lingxing OpenAPI）只读数据监控、销量/广告异常检测、飞书群自定义机器人通知与 MCP 查询服务。

> 当前阶段：第二轮基础集成。默认 `APP_DRY_RUN=true`、`FEISHU_ENABLED=false`、`SCHEDULER_ENABLED=false`。不提交真实密钥、不自动读取真实店铺、不自动向飞书群发消息。

## 当前架构

- **领星：** AppID / AppSecret 用于按账户文档配置 Token 获取和续约；业务 API 使用 Bearer Token + URL Query 签名。Token 路径、请求体和响应字段路径必须从当前领星开放平台文档确认后填写。
- **飞书：** 使用群自定义机器人 Webhook 单向推送到一个指定群，不使用飞书应用机器人或 tenant_access_token。
- **数据库：** MariaDB 11；本地测试支持 SQLite。保留原始快照、日指标、异常事件、通知日志和 outbox。
- **销售采集：** 已接入领星 ASIN/MSKU 日销量接口，分别采集销售额、销量、订单量，并保存店铺汇总和原始响应。
- **广告采集：** 已接入 SP/SB/SD 活动日报、SP/SB/SD 活动基础资料，以及 SP/SD 商品广告日报。SB 商品级报表接口尚未确认，不猜测接口路径。
- **数据完整性：** 按店铺、日期、数据维度写入 coverage 标记；只有完整采集日才能把未返回的指标解释为零。
- **时区：** 每个 sid 单独配置 IANA 时区和币种，日报按站点本地前一完整自然日计算。
- **异常：** 店铺状态异常事件；店铺、ASIN、MSKU 订单量、销量和销售额相对前七个完整自然日均值变化默认 ±15%；广告高花费、花费/点击达到门槛但无转化、低花费无转化、预算消耗和领星超预算事件。
- **可靠通知：** 幂等 outbox、worker claim、失败退避重试和投递日志。Webhook 本身不支持外部幂等键，因此进程在“飞书已接收、数据库尚未标记成功”之间崩溃时，重试可能产生重复消息（at-least-once）。
- **Excel：** 已生成真实数据的七工作表日报，支持全店汇总和按店铺分别导出。
- **调度/回补：** 每日调度已连接采集、异常分析和店铺 Excel 导出；提供 8–30 天显式历史回补 CLI。默认调度关闭。
- **MCP：** 提供只读店铺、产品/MSKU/ASIN负责人映射、SKU/ASIN与广告活动七日表现、日报汇总、异常历史、飞书投递日志、coverage 和同步任务查询；支持本地 stdio 与可选的 Streamable HTTP 远程服务。远程服务使用 Bearer Token，并默认只绑定宿主机回环地址；不开放领星采集或广告修改工具。

## 已核验的领星业务 API

| 功能 | 方法 / 路径 |
|---|---|
| 店铺列表 | GET `/erp/sc/data/seller/lists` |
| MSKU/ASIN 每日销量统计 | POST `/erp/sc/data/sales_report/asinDailyLists` |
| SP 活动资料 | POST `/pb/openapi/newad/spCampaigns` |
| SB 活动资料 | POST `/pb/openapi/newad/hsaCampaigns` |
| SD 活动资料 | POST `/pb/openapi/newad/sdCampaigns` |
| SP 活动日报 | POST `/pb/openapi/newad/spCampaignReports` |
| SB 活动日报 | POST `/pb/openapi/newad/hsaCampaignReports` |
| SD 活动日报 | POST `/pb/openapi/newad/sdCampaignReports` |
| SP 商品广告日报 | POST `/pb/openapi/newad/spProductAdReports` |
| SD 商品广告日报 | POST `/pb/openapi/newad/sdProductAdReports` |

广告接口使用 `X-API-VERSION: 2`。SP/SD 报表按 `next_token` 游标分页，SB 活动报表按 `offset/length` 分页；分页缺失或不完整时客户端会报错，不把部分结果当成完整日报。领星签名按 ASCII 排序，将业务参数、原始 `access_token`、`app_key`、`timestamp` 拼接后 MD5 大写，再使用 AppID 作为 AES 密钥执行 AES/ECB/PKCS5Padding。Bearer Token 通过 `Authorization` 头发送，但仍须参与签名；POST JSON 中的标量业务字段也须参与签名。当前客户端对嵌套对象/数组签名会 fail closed，不会猜测序列化规则。

官方资料：
- [领星 OpenAPI 接入指南](https://apidoc.lingxing.com/new/guides/new-instructions/)
- [领星开放平台](https://apidoc.lingxing.com/new)
- [店铺列表](https://apidoc.lingxing.com/new/api-docs/BasicData/SellerLists/)
- [MSKU/ASIN 销量统计](https://apidoc.lingxing.com/new/api-docs/Statistics/AsinDailyLists/)
- [SP 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/spCampaignReports/)
- [SB 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/hsaCampaignReports/)
- [SD 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/sdCampaignReports/)
- [SP 活动资料](https://apidoc.lingxing.com/new/api-docs/newAd/baseData/spCampaigns/)
- [SB 活动资料](https://apidoc.lingxing.com/new/api-docs/newAd/baseData/hsaCampaigns/)
- [SD 活动资料](https://apidoc.lingxing.com/new/api-docs/newAd/baseData/sdCampaigns/)
- [SP 商品广告报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/spProductAdReports/)
- [SD 商品广告报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/sdProductAdReports/)

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
   cp config/store-timezones.example.json data/store-timezones.json
   sudo chown -R 10001:10001 data
   chmod 700 data
   chmod 600 data/feishu-routing.json data/store-timezones.json
   nano data/store-timezones.json
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

远程 MCP 服务使用独立的可选 profile。先在服务器 `.env` 中生成并设置至少 32 字符的 Bearer Token：
```bash
openssl rand -hex 32
nano .env
# 设置 MCP_BEARER_TOKEN=上一步生成的随机值
```

保持默认 `MCP_BIND_IP=127.0.0.1`，再启动：
```bash
docker compose --profile mcp up -d --build mcp
docker compose logs -f mcp
```

本机 MCP 地址为 `http://127.0.0.1:8001/mcp`。如果要经公网域名提供给远程 MCP 客户端，先配置 TLS 反向代理，并把域名写入 `MCP_ALLOWED_HOSTS`（例如 `mcp.example.com,mcp.example.com:*`）；浏览器客户端还需配置准确的 `MCP_ALLOWED_ORIGINS`。不要将 MCP 容器端口直接暴露公网。客户端需要使用对应 Bearer Token。不要把 Token 写进仓库、聊天或 URL。

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

领星连接检查默认只检查本地配置，不获取 Token 或请求店铺数据。确认 Token 接口字段已核验后才加 `--request`：

```bash
python scripts/test_lingxing_connection.py
python scripts/test_lingxing_connection.py --request
```

每日全量采集默认 dry-run。确认 Token 配置和每个 sid 的时区后，显式使用 `--collect`，并将服务器本地 `APP_DRY_RUN=false`：
```bash
python scripts/collect_lingxing_daily.py
python scripts/collect_lingxing_daily.py --collect --date 2026-11-10
# 每个店铺使用自己的站点日期：
python scripts/collect_lingxing_daily.py --collect --date-map data/store-report-dates.json

# 历史回补：首次上线建议至少回补 8 天；默认回补 14 天，最多 30 天
python scripts/backfill_lingxing.py
python scripts/backfill_lingxing.py --collect --days 14

# 手动导出已有数据
python scripts/export_daily_report.py --date 2026-11-10
```

飞书连通性脚本默认只做本地配置检查，不发送消息。只有明确准备好向真实群发送测试时才加 `--send`：
```bash
python scripts/test_feishu_webhook.py
python scripts/test_feishu_webhook.py --send
```

## 尚未达到生产完成的部分

- 领星 Token 接口的准确路径、请求字段和响应字段路径尚需按你账号开放平台当前文档填写并验证。
- 领星账户健康/店铺绩效列表、绩效详情和业绩通知 API 尚未完成；需要核对这些接口的准确路径、请求参数和响应结构。
- SB 商品级广告日报的公开 API 契约尚未确认；当前不将 SB 活动数据分摊到商品。
- 产品表现异步报告（reportType=2）需要嵌套 reportOptions 的签名 canonicalizer；当前客户端对此类嵌套参数 fail closed，未猜测签名格式。
- 绩效通知接口尚未确认。
- 店铺/站点时区、币种、负责人映射需由真实账号数据确认。
- 数据库已加入兼容旧表的幂等增量升级；尚未迁移到完整 Alembic 版本化迁移框架。
- 尚未在你的 Linux 服务器执行 Docker 部署，也未进行真实领星、飞书或远程 MCP 客户端端到端测试。

第一阶段只读领星，不提供修改广告预算、竞价、关键词或活动状态的功能。
