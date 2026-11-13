# 使用 ChatGPT 调用 JX Alert

当前服务器已有受认证保护的内网 Streamable HTTP MCP。固定 Bearer 认证适用于支持自定义请求头的客户端；ChatGPT 自定义 MCP 需要 OAuth 流程。不能把固定 Token 粘到 URL，也不能选择“无认证”来绕过保护。

官方依据（2026-11-13核对）：

- [ChatGPT 自定义 MCP 的连接与使用](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OAuth、PKCE、资源与权限要求](https://developers.openai.com/plugins/build/auth)

## 服务器准备

1. 用户已指定专用域名 `jxmcp.pacefitday.com` 并解析到 `43.153.88.94`。本次增加专用虚拟主机；HTTPS、实际服务器版本及 ChatGPT 接入分别验收，详见 [小时分析设置](CHAT_ANALYSIS_SETUP.md)。
2. 使用仓库 `deploy/nginx-mcp.conf.example` 新建独立虚拟主机，替换域名和已有证书路径。不要覆盖现有站点。MCP 端口仍绑定 `127.0.0.1:8001`。
3. 在服务器交互初始化资源所有者口令。口令至少16字符，输入不会回显，只保存PBKDF2哈希；不修改 `.env`，不写Git，不在聊天中发送口令。

```bash
cd /opt/jx-alert-system
sudo docker exec -it jx-alert-app-1 python /app/scripts/setup_mcp_owner.py
```

4. 将 `/opt/jx-alert-system/data/runtime-settings.json` 中下列非敏感配置与现有字段合并，不能整文件覆盖：

```json
{
  "mcp_auth_mode": "oauth",
  "mcp_public_url": "https://jxmcp.pacefitday.com",
  "mcp_allowed_hosts": "jxmcp.pacefitday.com,jxmcp.pacefitday.com:443,127.0.0.1:*,localhost:*",
  "mcp_allowed_origins": "https://chatgpt.com,https://jxmcp.pacefitday.com",
  "mcp_forwarded_allow_ips": "127.0.0.1,REPLACE_WITH_VERIFIED_DOCKER_GATEWAY"
}
```

仅信任实际反代来源地址。通过 `docker inspect jx-alert-mcp-1` 的网络网关字段核实地址，不要把 `mcp_forwarded_allow_ips` 写成 `*`。宿主Nginx需要覆盖 `X-Forwarded-Proto`，不能透传用户伪造的值。OAuth要求真实HTTPS。

5. `nginx -t` 通过后加载该专用配置，重启 MCP 进程，验证未认证请求为401、资源和授权元数据正确、HTTPS登录与同意流程成功，再连接ChatGPT。不要在只有自签名证书时跳过浏览器警告。

OAuth采用SDK协议处理，授权码和refresh单次消费，refresh重放撤销令牌家族；access/refresh/code仅存哈希。DCR客户端secret因SDK鉴权需要保存在私有SQLite，文件0600。访问日志关闭以免记录授权码、state及查询字符串。当前是单所有者系统，不提供店铺间多用户权限隔离。

## ChatGPT 中添加

按官方当前入口：打开 **Plugins → ＋ → Add custom MCP server**，选择 **Server URL**，本项目地址只填写 `https://jxmcp.pacefitday.com/mcp`，认证选择 **OAuth**。Codex中相应的远程MCP连接也使用这个接口地址。使用动态客户端注册（DCR），不要把应用密钥当成OAuth客户端信息填写。创建并安装私有插件后，在对话中输入 `@` 选择它。实际界面名称可能随账户/工作区变化；能否创建还受工作区权限限制。

`/.well-known/oauth-authorization-server` 是授权信息页，`/.well-known/oauth-protected-resource/mcp` 是资源信息页；两者供客户端自动发现，**不能填入 Server URL 或 OAuth resource**。本项目的受保护资源固定为 `https://jxmcp.pacefitday.com/mcp`。错误地址会被拒绝并返回 `invalid_target`、正确的 `expected_resource`，不会为了连接成功放宽资源校验。

如果已创建连接后出现这个错误，请在该连接设置中更正 Server URL；若入口不能修改地址，则重新创建使用正确地址的连接，再从连接界面发起新的OAuth流程。不要手工修改浏览器中旧 `/authorize?...` 链接后继续使用；其中的 `state`、PKCE及回调信息属于那一次客户端发起的流程。服务端修正文档和错误提示不代表已经代用户修改了客户端连接设置。

若已经进入所有者口令页，提交后显示“授权请求无效或已过期”，2026-11-13 已修复一处浏览器表单来源问题：
原 `no-referrer` 策略会使原生表单发送 `Origin:null`，正常请求被同源校验拒绝。
登录与同意页改用 `same-origin`，并继续拒绝空来源、错误CSRF、过期请求和错误浏览器绑定。
关闭旧页，从插件设置发起新的连接，在10分钟内使用私下初始化的所有者口令登录并同意。
这里不填写飞书 App Secret、服务器密码或固定 Bearer token。服务端测试通过后仍需用户真实重连验收。

如果口令已通过、点击“同意”后仍出现同样错误，另有浏览器回调问题：
Chromium会把`form-action 'self'`应用到表单提交后的跨站302跳转。
首次同意可能已经签发授权码却被浏览器阻止回到ChatGPT；重复点击同一表单则因请求已消费而403。
修复只在**已登录的同意页及批准/拒绝重定向**允许该次注册回调的规范来源，
登录口令页仍限制同源提交。来源由严格校验的scheme/hostname/port构造，不拼接路径、查询或原始netloc，
防止回调URI注入CSP。精确redirect_uri、CSRF、单次消费、PKCE和resource校验继续保留。
必须从插件设置发起全新连接，不重试已经消费的旧“同意”按钮，也不重置所有者口令。

浏览器行为依据：[MDN form-action](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/form-action)。

授权页将明确显示权限：`jx:read` 查询已存数据；`jx:collect` 请求已配置范围的采集任务；`jx:notify` 将有证据的中文分析发送至已配置飞书群。仅查询的连接无需授予采集或通知权限。采集与通知还需相应服务器开关；所有领星业务操作只读，没有广告修改工具。

需要发送异常时，先用 `system_health` 查看 `current_connection_permissions.notify`。
只读连接调用通知工具会提示补授「发送飞书异常通知」权限。
如 ChatGPT 把该提示显示为连接过期，从连接提示发起授权，确认授权页包含「发送至已配置飞书群」后同意。
既有只读连接可发起该请求，刷新令牌继续遵守原授予的权限。

分析按同一对象和指标优先使用完整七日基线，七日不足时使用完整的前一日报表。
只有明确记录的零才作为零。缺数据、授权失败或仅部分指标可比时分别保留状态。
广告转化使用已完成归因的日期，通知卡片标明「较过去7日均值」或「较前一日」。

启用 `feishu_group_fallback_enabled` 后，缺少人员映射的通知直接发到已核实的日报群「独立站交接群」。
群兜底不带未核实的提及，同一异常发往同一群时合并。历史失败记录保留原状。

首先发送：

> @JX Alert 检查系统状态与需要我手工补齐的配置，再查询店铺5174最近完整日期的销量、广告花费和待确认对账差异。缺失记录标未知。

常用请求：

- “查询5174在2026-11-11的SP日报与小时对账，分别显示花费、点击和曝光差。”
- “查看最近异常，按店铺、活动、SKU/ASIN、严重级别和归因成熟度排表。”
- “检查飞书哪些通知送达、哪些因缺少管理员或运营映射未发送。”
- “仅检查数据覆盖和配置，不触发采集。”
- 获准 `jx:collect` 后：“为已配置店铺请求最近7天回补，幂等键 chat-backfill-20261113；返回任务号，再查询执行结果。”

可用工具包括 `system_health`、`get_configuration_status`、`get_analysis_plan`、`get_stores`、`get_campaigns`、`get_products`、日报/商品表现/异常/覆盖/任务/通知查询、`get_sp_report`、`get_sp_reconciliations`、受权限保护的 `request_collection`，以及证据分析与异常提交工具 `prepare_chat_analysis`、`submit_chat_analysis`。分析通知需要 `jx:notify`；空结论不会发送群消息。以连接实际列出的工具为准。全店铺模式详见[全店铺说明](ALL_STORE_ANALYSIS.md)。

域名、TLS与所有者口令填好后，仍必须在ChatGPT实际完成授权并成功调用一次；单元测试或服务器本机初始化不能替代该项外部验收。

## 飞书通道状态

`system_health`和`get_configuration_status`按`feishu_transport`报告实际选用的通道。
`app`表示自建应用，凭据可由现有私有文件加载，此模式不需要Webhook。
兼容字段`feishu_webhook_configured=false`仅表示默认Webhook未配置，不能据此说自建应用未连接。
请使用`feishu_transport_configured`和`feishu_status_note`判断当前通道配置；
`feishu_delivery_enabled`仅表示本机开关允许执行，不证明某条消息已发送或某个接收人已映射。
检查不请求飞书令牌、不发送测试消息；实际成功以通知日志和outbox的`DELIVERED`为准。
未知通道或应用凭据无效不会自动退回Webhook，也不返回密钥、路径或原始错误。

## 本地原生浏览器回归

`scripts/verify_oauth_browser.py` 使用全新隔离的Chrome／Chromium profile，在loopback临时TLS服务中验证原生登录、同意、跨源回调与PKCE换令牌；不连接生产、不读取用户浏览器或生产凭据。
先运行负例，再运行正例：

```bash
python scripts/verify_oauth_browser.py --expect-blocked --output work/oauth-browser-verification.json
python scripts/verify_oauth_browser.py --output work/oauth-browser-verification.json
```

自动发现不到浏览器时传 `--browser /path/to/chrome`。脚本仅对临时公钥使用SPKI信任，不关闭CSP或浏览器安全；有32秒超时保护，并关闭自己创建的隐藏浏览器。产物只含阶段和验证状态，不包含口令、授权码、令牌或完整授权URL。
