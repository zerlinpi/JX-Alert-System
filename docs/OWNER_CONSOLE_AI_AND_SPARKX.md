# 运营台：DeepSeek、SparkX MCP 与运营姓名

## DeepSeek 设置（分析设置）

- 「分析设置 → DeepSeek 设置」填写 API 密钥、选择模型（`deepseek-flash` 或 `deepseek-v4-pro`，与[官方模型列表](https://api-docs.deepseek.com/quick_start/pricing)一致）和超时。保存后下一次分析即使用新设置，不需要改环境变量或重启。
- 页面显示**密钥来源**（运营台保存 / 服务器环境变量 / 服务器私有文件）和**模型来源**。运营台保存过密钥后，它优先于环境变量；只改模型或超时、密钥留空时，不会把环境变量里的密钥复制到磁盘。
- 「测试 DeepSeek 连接」只调用官方 [List Models](https://api-docs.deepseek.com/api/list-models) 和 [Get Balance](https://api-docs.deepseek.com/api/get-user-balance)，不发送分析请求、不消耗 Token。结果只给出结论：连接正常、密钥被拒（401）、余额不足、所选模型不可用、超时或无法连接；不回显密钥或供应端原文。

## SparkX MCP 连接（分析设置）

[SparkX AI MCP](https://github.com/SparkXAI/SparkXAI-MCP) 是 Streamable HTTP 服务（`https://mcp.sparkx.cn/mcp`），使用 `Authorization: Bearer <MCP Token>`。JX 只调用两个只读工具：`get_user_authorized_context`（核对授权店铺）和 `get_ads_perf`（广告效果）。SparkX 同时提供创建/修改广告、预算、竞价的写工具，JX 的转发白名单不包含它们，本地即拒绝。

1. 在 SparkX 后台右上角账号菜单 →「MCP & Skills」→「新建 Token」，只勾选需要的店铺和广告效果只读范围（`amazon_sa_performance_data:read`）。Token 关闭弹窗后无法再次查看。
2. 运营台「SparkX MCP 连接」粘贴 Token，保存。Token 写入服务器私有文件 `data/secrets/sparkx.json`（0600），不会回显到浏览器、日志或 Git；留空保存表示保留现有 Token。运营台保存的值优先于环境变量 `SPARKX_AI_TOKEN`。
3. 点「测试 SparkX 连接」：调用 `get_user_authorized_context`，列出该 Token 授权的店铺 Profile（ID、名称、站点）。不显示 SparkX 的 userId / tenantId。
4. 把要用的 Profile ID 填进「允许使用的 Profile ID」并保存（可点「把以上全部授权店铺填入允许列表」）。之后：
   - 每天 08:30 的 SparkX 广告日报和广告异常，见[SparkX 广告日报与广告异常](SPARKX_DAILY_REPORT.md)；
   - 店铺页「商品推广（SP）观测来源」逐店核验并切换小时观测，规则见[逐店 SP 观测来源](AD_SOURCE_CONTROLS.md)。

失败时页面给出分类结论，只依据 HTTP 状态和错误类型，不读取供应端正文：

| 结论 | 含义 |
| --- | --- |
| Token 被拒（401） | Token 无效、过期、被禁用或缺少只读权限 |
| 拒绝访问（403） | Token 授权范围不包含所需内容 |
| 限流（429）/ 服务异常（5xx） | 稍后重试 |
| 超时 / 无法连接 | 服务器到 `mcp.sparkx.cn` 的网络、DNS 或防火墙问题 |

运营台保存的私有文件若损坏或权限不安全，SparkX 视为未配置（不会悄悄改用环境变量里的另一个 Token）。独立的 SparkX 转发试点容器（`docker-compose.sparkx.yml`）不挂载数据目录，仍只使用它自己的私有环境文件。

### 2026-11-23 可调用性核查

- 端点在线：未带凭据请求 `https://mcp.sparkx.cn/mcp` 返回 401，`WWW-Authenticate` 指向 `https://mcp.sparkx.cn/.well-known/oauth-protected-resource/mcp`，范围 `amazon_sa_performance_data:read`；带无效 Token 返回 401 `invalid_token`。
- JX 的 MCP 客户端（`mcp` 1.30.0，Streamable HTTP）能完成到该端点的请求并正确识别 401；查询字段（`profileIds`、`factEntity`、`dateStart/dateEnd`、`metrics`、`userContext`、`timeGranularity` 等）和响应结构（`data.rows`、`meta.hasNextPage`、`meta.effectiveProfileIds`、`meta.currency`）与官方 Skill 文档一致。
- 尚未用真实 Token 调通：服务器当时没有配置 SparkX Token。配置后用「测试 SparkX 连接」或服务器上的 `python -m jx_alert.sparkx_direct` 验证。

## 运营姓名（负责人映射）

告警、09:00 日报的广告页和 08:30 公司日报都会在每个广告活动/店铺旁写“对应运营：姓名”。姓名来自「负责人映射」里该店负责人的飞书群成员名：

- 逐店保存负责人时自动记录姓名。
- 「同步全部运营姓名」一次性读取各店所在群的成员，只补齐或更新已映射负责人的姓名；不增删负责人、不改通知对象。不在群里的人员姓名保持原样并计入“未更新”数量。
- 未映射负责人的店铺显示“未映射运营”，已映射但没有姓名的显示“已映射（姓名待同步）”。这些姓名只用于显示，不产生 @ 或私聊。
