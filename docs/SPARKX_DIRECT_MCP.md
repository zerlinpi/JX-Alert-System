# SparkXAI MCP 原样转发（隔离试点）

JX 可以为原生 SparkXAI 广告及支持的 ASIN 经营查询提供一个独立、只读的 MCP 入口。它使用官方 Streamable HTTP 地址，不需要安装桌面 Plugin，也不运行 SparkX 的读写 Skills。JX 不做二次 AI 分析、不换算指标、不补零、不混入领星数据，也不重新排版 MCP 的文本或结构化结果。

| JX 功能 | 本次覆盖 |
| --- | --- |
| 手动 MCP 广告/ASIN 指标 | 转发 SparkX 原生 `get_ads_perf`，保留原生结果、字段名与分页 |
| SP / SB / SD | 由 SparkX 查询、授权和数据覆盖决定；返回空结果不代表业务为零 |
| 账户健康、违规通知、完整店铺/MSKU/结算口径 | 无已公开、可等价替代的 SparkX 接口，不切换 |
| 飞书日报、每小时告警、机器人和 Excel | 未切换。公开周报/月报 Skills 由 AI 组织数据并排版，不是供应端成品报告 |
| 广告、预算、竞价、托管组写入 | 不支持；中转不给客户端暴露任何写工具 |

官方参考：[安装与权限](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/docs/installation.md)、[查询字段](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/SKILL.md)、[周报编排](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-weekly-ads-report/SKILL.md)。

## 验证与启用

1. 在 SparkX 生成**仅允许选定 Amazon Ads profile 和广告效果只读范围**的 MCP Token（`amazon_sa_performance_data:read`），在部署端私下设置 `SPARKX_AI_TOKEN`。不要把 Token 发到聊天、写入 Git、放进请求正文或日志。安装说明的桌面 Plugin 同时包含写功能，不要在 JX 服务端安装它。
2. 设置 `SPARKX_PROFILE_IDS` 为审核过的 SparkX 广告 profile ID 逗号列表，不是 JX 店铺 SID。每次查询只能选其中一个 profile，服务端仍会再次校验授权；需由管理员先核对广告账户、站点、币种和角色。未审核访问者不得拿到中转入口。
3. 先运行 `python -m jx_alert.sparkx_direct`。它通过官方 `get_user_authorized_context` 验证指定范围，只打印确认的数量，不打印账户目录或凭据。运行环境须能直连 `https://mcp.sparkx.cn/mcp`；中转不跟随重定向，也不读取系统代理配置。
4. 仅在隔离试点 MCP 进程设置 `MCP_OUTPUT_MODE=sparkx_direct`。HTTP 入口用 `MCP_AUTH_MODE=bearer`，另设 `SPARKX_RELAY_BEARER_TOKEN`（至少 32 字符，不得与 JX 原入站 Token 或 SparkX 上游 Token 相同），绑定回环或受控私有入口。不要替换现有生产 MCP、日报或 Scheduler。默认 `MCP_OUTPUT_MODE=jx` 保留现有行为。不要在多人共享进程运行 stdio 中转；stdio 的访问边界是启动进程所在的账号。
5. 用一个批准的测试 profile 比较直接调用 SparkX 和经 JX 中转后的原生 `content` / `structuredContent`，再验证未知 profile、写工具及领星/旧 JX 工具均不可用。不要把其他人、订单或凭据的信息放在 `userContext`。还没验证真实授权与数据覆盖前，不得关闭现有通知。

## 调用示例

```json
{
  "arguments": {
    "profileIds": [1234567890123456],
    "factEntity": "campaign",
    "dateStart": "2026-10-01",
    "dateEnd": "2026-10-07",
    "metrics": ["Impressions", "Clicks", "Spend", "Sales", "Conversions"],
    "select": ["date", "campaign.campaignType_"],
    "language": "zh",
    "userContext": "按日查看广告表现",
    "page": 1,
    "pageSize": 100
  }
}
```

这是指向中转工具 `get_ads_perf` 的 MCP 参数，不是候选 profile 的真实 ID。实际数据日期、归因口径和空记录以供应端为准。传输异常直接报错，不回退领星。若要停用，停止独立中转进程或撤销专用凭据，现有数据卷和 JX 主服务不受影响。
