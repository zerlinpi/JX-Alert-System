# 领星 OpenAPI Token 配置

## 1. 先完成领星后台准备

在领星 ERP 使用超级管理员账号进入「设置 → 业务配置 → 全局 → 开放接口」，确认 AppID、AppSecret，并把 Linux 服务器的公网出口 IP 加入 API IP 白名单。

AppSecret 有权访问企业数据，不能发到聊天、工单或公开仓库。

## 2. 当前客户端已处理的业务 API 认证

业务接口请求：
- API Host：`https://openapi.lingxing.com`
- `app_key` 就是领星 AppID，不需要额外申请第三个凭据；代码默认使用 AppID，只有显式配置 LINGXING_APP_KEY 时才覆盖。
- `Authorization: Bearer <access_token>`（官方当前推荐方式；2027-01-01 起 URL Query 传 access_token 将下线）。
- Query 包含 `app_key`、`timestamp`、`sign`；签名集合仍必须包含原始 access_token 一次，但 Authorization 字段名和 Bearer 前缀不参与签名。
- 签名参数包含业务参数、原始 `access_token`、`app_key`、`timestamp`，ASCII 排序后拼接，MD5 大写，再使用 AppID 做 AES/ECB/PKCS5Padding。
- POST JSON 的标量字段也参与签名。
- 领星令牌桶限流错误码 3001008 会进行有界指数退避重试；网络超时、HTTP 429 和 5xx 也会重试。
- 当前客户端对嵌套对象或数组参数拒绝猜测签名序列化方式；遇到此类 API 必须根据领星官方示例增加专用 canonicalizer 后再启用。

## 3. 配置 Token 获取与续约接口

领星接入指南将 Token 获取和续约作为独立认证接口。由于不同版本文档页面中的路径、参数名和响应字段必须以你账号当前开放平台显示为准，本项目没有硬编码猜测路径。

在领星 OpenAPI 文档中打开「获取接口令牌 access_token」和「续约接口令牌」页面，核对以下信息：
1. 请求方法（GET 或 POST）。
2. API Path。
3. 必填请求字段及其准确名称。
4. 响应中 access_token、refresh_token 和有效期字段的 JSON 层级。
5. Token 续约是否使用 refresh_token，以及 refresh_token 是否轮换。

将真实路径和请求模板填写到服务器本地 `.env`。请求模板支持以下字符串占位符：
- `${LINGXING_APP_ID}`
- `${LINGXING_APP_SECRET}`
- `${LINGXING_ACCESS_TOKEN}`
- `${LINGXING_REFRESH_TOKEN}`

例如，下面只是说明占位符写法的结构示意，不是领星真实 API 请求字段：
```env
LINGXING_TOKEN_PATH=
LINGXING_TOKEN_METHOD=POST
LINGXING_TOKEN_REQUEST_JSON={}
LINGXING_TOKEN_RESPONSE_PATH=
LINGXING_REFRESH_PATH=
LINGXING_REFRESH_METHOD=POST
LINGXING_REFRESH_REQUEST_JSON={}
LINGXING_REFRESH_RESPONSE_PATH=
LINGXING_EXPIRES_IN_RESPONSE_PATH=
```

响应字段路径使用点分隔 JSON 路径，例如 `data.access_token`；只有在你实际 API 响应确实如此时才填写。有效期字段必须返回 TTL 秒数。不要把示例路径直接复制为真实配置。

## 4. Token 缓存

成功取得 Token 后，系统将 access_token、refresh_token 和 expires_at 写入 `LINGXING_TOKEN_STORE_PATH` 指定的本地 JSON 文件，默认位于 `/app/data/secrets/lingxing-token.json`，并设置文件权限 0600。该文件所在的 `data/` 目录必须持久化并限制主机访问权限。Token Provider 还会在同目录创建权限为 0600 的锁文件，通过 Linux 文件锁串行化 API、scheduler、MCP 多容器的刷新操作，避免两个进程同时消费同一个一次性 refresh_token。

如果设置了 refresh_token 和续约路径，Token Provider 会在到期前尝试续约；续约返回的新 refresh_token 会替换旧值。refresh_token 若为一次性令牌，切勿手动重复使用旧值。

## 5. 真实 API 验证前的限制

当前还没有对你的账号执行真实 Token 获取或数据接口请求。请先提供（可打码 AppID、AppSecret 和 Token）Token 获取/续约文档中的：
- API Path 和请求方法；
- 请求参数字段名称；
- 成功响应 JSON 示例（凭据值全部打码）。

根据这三项信息核对本项目配置后，先在 Linux 服务器本地执行 dry-run 检查：
```bash
python scripts/test_lingxing_connection.py
```

确认 Token 路径、请求参数和响应字段路径已按官方文档填写后，才执行一次真实的只读店铺列表请求：
```bash
python scripts/test_lingxing_connection.py --request
```

该脚本不会打印 Token 或 AppSecret。不要把 AppSecret 或真实 Token 发给 ChatGPT。

## 6. 已接入的只读数据采集

Token 获取和只读店铺列表验证成功后，当前每日流水线会采集：

- 店铺列表和店铺状态；
- ASIN/MSKU 销售额、销量、订单量（type=1/2/3）；
- SP/SB/SD 广告活动基础资料与预算；
- SP/SB/SD 广告活动级日报；
- SP/SD 商品广告日报（按广告商品 ID 映射到 ASIN/MSKU）；
- 原始响应、标准化日指标和每日 coverage 完整性标记；店铺绩效列表/详情和业绩通知三个只读 OpenAPI 也已接入，保存健康评分、状态、平台标准、违规、通知状态及新鲜度。绩效采集默认关闭，确认源时区和真实脱敏响应后再启用。

SB 商品级广告报表的公开 API 契约尚未核实，因此当前不将 SB 活动级花费或订单分摊到 SKU。店铺绩效和业绩通知 OpenAPI 已接入；剩余生产验证包括源时区、真实响应字段和分页行为。

## 7. 配置站点时区

自动调度不会猜测站点日期。先复制时区模板并按真实 sid 修改：
```bash
cp config/store-timezones.example.json data/store-timezones.json
chmod 600 data/store-timezones.json
nano data/store-timezones.json
```

示例：
```json
{
  "stores": {
    "101": {
      "timezone": "America/Los_Angeles",
      "currency_code": "USD"
    }
  }
}
```

每个正常同步的店铺都必须填写正确的 IANA 时区。日报使用该店铺本地前一完整自然日。不同站点时区不同，不要用单一北京时间替代。

## 8. 只读连通性与历史回补

先运行不发请求的本地配置检查：
```bash
python scripts/test_lingxing_connection.py
```

Token 路径、请求参数和响应字段均已按当前账号开放平台文档核对后，才运行一次只读店铺列表请求：
```bash
python scripts/test_lingxing_connection.py --request
```

首次启用前建议回补至少 8 天，默认 14 天，最多 30 天。回补命令默认 dry-run：
```bash
python scripts/backfill_lingxing.py
```

确认 APP_DRY_RUN=false、Token 已验证、时区映射完整后，显式执行：
```bash
python scripts/backfill_lingxing.py --collect --days 14
```

回补会按每个 sid 的站点日期采集数据，生成每日任务日志、coverage 标记、异常分析和每店 Excel 文件。它只读取领星，不会修改广告或店铺数据。采集任务失败时会记录 FAILED/PARTIAL_FAILED；不完整日期不会触发异常自动恢复。

## 9. 每日调度

历史回补、规则和报表检查通过后，才在 .env 中设置：
```env
APP_DRY_RUN=false
SCHEDULER_ENABLED=true
DAILY_SYNC_CRON=0 8 * * *
STORE_TIMEZONES_FILE=/app/data/store-timezones.json
```

如需向飞书真实群发送通知，再单独配置并测试：
```env
FEISHU_ENABLED=true
FEISHU_WEBHOOK_URL=你的群自定义机器人Webhook
FEISHU_SIGN_SECRET=机器人签名密钥（若启用）
```

启动 scheduler：
```bash
docker compose --profile scheduler up -d --build scheduler
docker compose logs -f scheduler
```

只允许一个 scheduler 实例。它会按店铺时区执行日报采集、异常分析、每店 Excel 导出，并每 10 秒处理通知 outbox。

## 10. 当前未完成的领星接口

- 产品表现异步报告（reportType=2）需要 reportOptions 嵌套对象的签名 canonicalizer；当前客户端对嵌套对象/数组 fail closed，未猜测签名格式。
- 店铺绩效列表、详情和业绩通知列表已按已确认路径接入；仍需使用真实脱敏响应核验账户权限、字段映射、分页与更新时间源时区。
- SB 商品级广告报表接口尚未核实。

如需继续接入这些接口，请从领星 OpenAPI 文档复制对应页面的 API Path、请求参数表和成功响应 JSON 示例；打码账号、Token 和其他凭据即可。不要发送 AppSecret 或真实 Token。
