# JX Alert System

领星 ERP（Lingxing OpenAPI）数据采集、销量/广告异常检测、飞书定向通知与 MCP 服务。

> 当前版本：bootstrap scaffold（首个可运行骨架）。系统默认 **dry-run**，不会自动连接真实领星账号或发送飞书消息。真实凭据、飞书应用、店铺负责人映射和 token 获取/续约配置完成后，再启用生产任务。

## 目标

- 只读接入领星 OpenAPI：店铺、MSKU 销量、SP/SB/SD 活动报表、活动基础资料、店铺绩效/业绩通知（绩效接口路径需按账户可用文档确认）。
- 按站点日期保存原始 JSON 和标准化日快照。
- 每日比较昨日与前一日；异常基准为此前 7 个完整自然日，不把检测日纳入基准。
- 订单变化阈值默认 ±15%；广告高花费、达到门槛但无转化、低花费无转化、预算消耗预警可配置。
- 异常生命周期：NEW / ONGOING / ESCALATED / RECOVERED / CLOSED；可靠 outbox、幂等键、投递日志。
- 飞书自建应用向指定群发消息，并使用真实 open_id @ 管理员和负责人。
- 生成每日多工作表 Excel；数据库是事实来源，Excel 是归档/人工查看副本。
- 独立调度器运行每日采集与分析；MCP 提供查询、导出和受控手动触发入口。

## 技术栈

Python 3.12、FastAPI、SQLAlchemy 2、MySQL 8 / MariaDB 11、APScheduler、openpyxl、MCP Python SDK、Docker Compose。

## 重要的领星 API 说明

已按领星官方文档核验的接口：

| 功能 | 方法 / 路径 |
|---|---|
| 店铺列表 | GET `/erp/sc/data/seller/lists` |
| 按 MSKU/ASIN 的每日销售额/销量/订单量 | POST `/erp/sc/data/sales_report/asinDailyLists` |
| SP 活动日报 | POST `/pb/openapi/newad/spCampaignReports` |
| SB 活动日报 | POST `/pb/openapi/newad/hsaCampaignReports` |
| SD 活动日报 | POST `/pb/openapi/newad/sdCampaignReports` |
| SP 活动资料 | POST `/pb/openapi/newad/spCampaigns` |
| SB 活动资料 | POST `/pb/openapi/newad/hsaCampaigns` |
| SD 活动资料 | POST `/pb/openapi/newad/sdCampaigns` |

广告接口使用 `X-API-VERSION: 2` 游标分页（`next_token`）。领星业务接口签名：业务参数 + `access_token` + `app_key` + `timestamp` 按 ASCII 排序拼接，MD5 大写后用 AES/ECB/PKCS5Padding（密钥为 AppID）加密；业务请求使用 `Authorization: Bearer <access_token>`，签名仍包含原始 token。Token 获取/续约接口路径和参数应以账号开放平台当前文档为准，因此由 `LINGXING_TOKEN_PATH`、`LINGXING_REFRESH_PATH` 与请求体 JSON 配置，不在代码中猜测路径。

官方文档：
- [领星 OpenAPI 文档](https://apidoc.lingxing.com/new)
- [领星 API 接入指南](https://apidoc.lingxing.com/new/guides/new-instructions/)
- [店铺列表](https://apidoc.lingxing.com/new/api-docs/BasicData/SellerLists/)
- [MSKU/ASIN 销量统计](https://apidoc.lingxing.com/new/api-docs/Statistics/AsinDailyLists/)
- [SP 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/spCampaignReports/)
- [SB 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/hsaCampaignReports/)
- [SD 活动报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/sdCampaignReports/)

## 快速开始

1. 复制环境变量模板：`cp .env.example .env`，填写数据库、领星和飞书配置。
2. 先保持 `APP_DRY_RUN=true`、`FEISHU_ENABLED=false`。
3. 启动：`docker compose up --build -d`。
4. 查看健康状态：`curl http://localhost:8000/health`。
5. 执行测试：`docker compose run --rm app pytest`。
6. 配置真实 token 路径、领星 AppID/AppSecret、服务器固定公网 IP 白名单、飞书应用权限及路由表后，先手动跑连接探测，再打开采集和通知。

## 项目目录

- `src/jx_alert/lingxing`：签名、token、只读 API 客户端、采集适配器。
- `src/jx_alert/analysis`：确定性规则引擎和异常生命周期。
- `src/jx_alert/notifications`：飞书消息、outbox 和投递重试。
- `src/jx_alert/storage`：SQLAlchemy 模型与数据库。
- `src/jx_alert/reporting`：Excel 导出。
- `src/jx_alert/mcp_server.py`：MCP tools。
- `src/jx_alert/scheduler.py`：独立每日调度。
- `tests`：离线单元测试。

## 生产启用前检查

- [ ] 在领星开放平台申请 AppID/AppSecret，并把服务器出口 IP 加入白名单。
- [ ] 根据账户实际文档配置 token 获取与续约路径/参数；验证签名与返回字段。
- [ ] 核对店铺日期、站点时区、币种和广告归因口径。
- [ ] 在飞书创建企业自建应用，授予机器人发消息权限并加入目标群。
- [ ] 填写 chat_id、管理员 open_id、店铺/产品负责人 open_id 路由。
- [ ] 完成 7–30 天历史回补；确保 7 天基准完整后再启用异常通知。
- [ ] 先 dry-run 验证日报、异常和 Excel，再启用真实消息。
- [ ] 设置数据库备份、日志保留、监控和告警。

## 安全边界

第一阶段只读领星，不提供修改广告预算、竞价、关键词或活动状态的工具。凭据只从环境变量读取，严禁提交 `.env`、token、AppSecret、飞书 App Secret 或真实店铺数据。真实采集和真实群消息必须由部署者显式启用。

## 状态

此仓库初始为空仓库。本次开发从基础可运行骨架开始；README 中标明“待配置/待核验”的接口不会被伪装成已验证的生产集成。
