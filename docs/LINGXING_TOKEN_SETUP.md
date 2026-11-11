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

### 3.1 获取 access_token（已按官方文档确认）

当前领星 OpenAPI「获取 access-token 和 refresh-token」文档确认：

- Method：`POST`
- API Path：`/api/auth-server/oauth/access-token`
- Content-Type：`multipart/form-data`
- 表单字段：`appId`、`appSecret`
- 成功响应状态码：字符串 `"200"`
- Access Token：`data.access_token`
- Refresh Token：`data.refresh_token`
- 有效期秒数：`data.expires_in`

项目已支持三种 POST 编码：`json`、`form`（application/x-www-form-urlencoded）和 `multipart`。multipart 使用普通表单字段，并由 HTTP 客户端自动生成 boundary；不要手动设置 Content-Type。

服务器 `.env` 中配置：

```env
LINGXING_TOKEN_PATH=/api/auth-server/oauth/access-token
LINGXING_TOKEN_METHOD=POST
LINGXING_TOKEN_REQUEST_FORMAT=multipart
LINGXING_TOKEN_REQUEST_JSON={"appId":"${LINGXING_APP_ID}","appSecret":"${LINGXING_APP_SECRET}"}
LINGXING_TOKEN_RESPONSE_PATH=data.access_token
LINGXING_REFRESH_RESPONSE_PATH=data.refresh_token
LINGXING_EXPIRES_IN_RESPONSE_PATH=data.expires_in
```

请求模板支持以下字符串占位符：
- `${LINGXING_APP_ID}`
- `${LINGXING_APP_SECRET}`
- `${LINGXING_ACCESS_TOKEN}`
- `${LINGXING_REFRESH_TOKEN}`

不要把真实 AppSecret 或 Token 写入仓库。只在服务器本地 `.env` 中填写 AppID 和 AppSecret，并将文件权限限制为仅应用用户可读写（例如 `chown 10001:10001 .env && chmod 600 .env`）。

### 3.2 Refresh Token 续约（仍需官方文档确认）

不要猜测续约接口路径、请求方式或参数名。请在当前领星 OpenAPI 文档打开「续约接口令牌」，确认 Method、API Path、请求字段、成功响应 JSON、refresh_token 是否轮换及有效期字段后再配置。

在续约接口核实之前，保持：

```env
LINGXING_REFRESH_PATH=
LINGXING_REFRESH_METHOD=POST
LINGXING_REFRESH_REQUEST_FORMAT=json
LINGXING_REFRESH_REQUEST_JSON={}
```

在没有配置续约路径时，Token Provider 会在 access_token 失效后使用 AppID/AppSecret 重新获取令牌；不会假定或调用未核实的 refresh 接口。拿到官方续约文档后，再配置 `LINGXING_REFRESH_PATH`、请求格式、请求模板和对应响应字段映射。

### 3.3 Token 缓存与请求编码

成功取得 Token 后，系统将 access_token、refresh_token 和 expires_at 写入 `LINGXING_TOKEN_STORE_PATH` 指定的本地 JSON 文件，默认位于 `/app/data/secrets/lingxing-token.json`，并设置文件权限 0600。该文件所在的 `data/` 目录必须持久化并限制主机访问权限。Token Provider 还会在同目录创建权限为 0600 的锁文件，通过 Linux 文件锁串行化 API、scheduler、MCP 多容器的刷新操作，避免两个进程同时消费同一个一次性 refresh_token。

请求格式通过以下配置控制：
- `LINGXING_TOKEN_REQUEST_FORMAT=multipart`：首次获取令牌使用 multipart/form-data。
- `LINGXING_REFRESH_REQUEST_FORMAT=json`：续约请求默认 JSON；只有官方续约文档确认后才调整。

GET 请求始终通过 URL Query 发送参数。POST 请求支持 JSON、URL-encoded form 和 multipart/form-data。multipart 字段必须是非空的标量值，不接受嵌套对象或数组。

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
