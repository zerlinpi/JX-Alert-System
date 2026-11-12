# 使用 ChatGPT 调用 JX Alert

当前服务器已有受认证保护的内网 Streamable HTTP MCP。固定 Bearer 认证适用于支持自定义请求头的客户端；ChatGPT 自定义 MCP 需要 OAuth 流程。不能把固定 Token 粘到 URL，也不能选择“无认证”来绕过保护。

官方依据（2026-11-13核对）：

- [ChatGPT 自定义 MCP 的连接与使用](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OAuth、PKCE、资源与权限要求](https://developers.openai.com/plugins/build/auth)

## 服务器准备

1. 用户提供专用域名，将 DNS A 记录指向 `43.153.88.94`，确认公网有效 TLS 证书。域名尚未提供，因此当前没有修改既有网站或开放公网入口。
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
  "mcp_public_url": "https://mcp.example.com",
  "mcp_allowed_hosts": "mcp.example.com,mcp.example.com:443,127.0.0.1:*,localhost:*",
  "mcp_allowed_origins": "https://chatgpt.com,https://mcp.example.com",
  "mcp_forwarded_allow_ips": "127.0.0.1,REPLACE_WITH_VERIFIED_DOCKER_GATEWAY"
}
```

仅信任实际反代来源地址。通过 `docker inspect jx-alert-mcp-1` 的网络网关字段核实地址，不要把 `mcp_forwarded_allow_ips` 写成 `*`。宿主Nginx需要覆盖 `X-Forwarded-Proto`，不能透传用户伪造的值。OAuth要求真实HTTPS。

5. `nginx -t` 通过后加载该专用配置，重启 MCP 进程，验证未认证请求为401、资源和授权元数据正确、HTTPS登录与同意流程成功，再连接ChatGPT。不要在只有自签名证书时跳过浏览器警告。

OAuth采用SDK协议处理，授权码和refresh单次消费，refresh重放撤销令牌家族；access/refresh/code仅存哈希。DCR客户端secret因SDK鉴权需要保存在私有SQLite，文件0600。访问日志关闭以免记录授权码、state及查询字符串。当前是单所有者系统，不提供店铺间多用户权限隔离。

## ChatGPT 中添加

按官方当前入口：打开 **Plugins → ＋ → Add custom MCP server**，选择 **Server URL**，地址填写 `https://你的域名/mcp`，认证选择 **OAuth**。使用动态客户端注册（DCR），不要把应用密钥当成OAuth客户端信息填写。创建并安装私有插件后，在对话中输入 `@` 选择它。实际界面名称可能随账户/工作区变化；能否创建还受工作区权限限制。

授权页将明确显示权限：`jx:read` 查询已存数据；`jx:collect` 请求已配置范围的采集任务。仅查询的连接不要授予采集权限。采集仍须服务器启用操作开关；所有领星业务操作只读，没有广告修改工具。

首先发送：

> @JX Alert 检查系统状态与需要我手工补齐的配置，再查询店铺5174最近完整日期的销量、广告花费和待确认对账差异。缺失记录标未知。

常用请求：

- “查询5174在2026-11-11的SP日报与小时对账，分别显示花费、点击和曝光差。”
- “查看最近异常，按店铺、活动、SKU/ASIN、严重级别和归因成熟度排表。”
- “检查飞书哪些通知送达、哪些因缺少管理员或运营映射未发送。”
- “仅检查数据覆盖和配置，不触发采集。”
- 获准 `jx:collect` 后：“为已配置店铺请求最近7天回补，幂等键 chat-backfill-20261113；返回任务号，再查询执行结果。”

可用工具包括 `system_health`、`get_configuration_status`、`get_stores`、`get_campaigns`、`get_products`、日报/商品表现/异常/覆盖/任务/通知查询、`get_sp_report`、`get_sp_reconciliations` 和受权限保护的 `request_collection`。以连接实际列出的工具为准。

域名、TLS与所有者口令填好后，仍必须在ChatGPT实际完成授权并成功调用一次；单元测试或服务器本机初始化不能替代该项外部验收。
