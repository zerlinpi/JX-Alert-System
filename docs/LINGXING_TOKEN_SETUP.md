# 领星 OpenAPI Token 配置

## 1. 先完成领星后台准备

在领星 ERP 使用超级管理员账号进入「设置 → 业务配置 → 全局 → 开放接口」，确认 AppID、AppSecret，并把 Linux 服务器的公网出口 IP 加入 API IP 白名单。

AppSecret 有权访问企业数据，不能发到聊天、工单或公开仓库。

## 2. 当前客户端已处理的业务 API 认证

业务接口请求：
- API Host：`https://openapi.lingxing.com`
- `Authorization: Bearer <access_token>`
- Query 包含 `app_key`、`timestamp`、`sign`
- 签名参数包含业务参数、原始 `access_token`、`app_key`、`timestamp`，ASCII 排序后拼接，MD5 大写，再使用 AppID 做 AES/ECB/PKCS5Padding。
- POST JSON 的标量字段也参与签名。
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

成功取得 Token 后，系统将 access_token、refresh_token 和 expires_at 写入 `LINGXING_TOKEN_STORE_PATH` 指定的本地 JSON 文件，默认位于 `/app/data/secrets/lingxing-token.json`，并设置文件权限 0600。该文件所在的 `data/` 目录必须持久化并限制主机访问权限。

如果设置了 refresh_token 和续约路径，Token Provider 会在到期前尝试续约；续约返回的新 refresh_token 会替换旧值。refresh_token 若为一次性令牌，切勿手动重复使用旧值。

## 5. 真实 API 验证前的限制

当前还没有对你的账号执行真实 Token 获取或数据接口请求。请先提供（可打码 AppID、AppSecret 和 Token）Token 获取/续约文档中的：
- API Path 和请求方法；
- 请求参数字段名称；
- 成功响应 JSON 示例（凭据值全部打码）。

根据这三项信息核对本项目配置，再进行单接口只读连通性测试。不要把 AppSecret 或真实 Token 发给 ChatGPT。
