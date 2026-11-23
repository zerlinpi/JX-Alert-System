# 管理台

地址：`CONSOLE_PUBLIC_URL` 的 `/console/`（HTTPS）。用所有者口令登录（沿用原 `mcp-owner.json`，口令不变），会话 30 分钟。所有写操作都需要同源请求和页面令牌。

## 页面

| 页面 | 作用 |
|---|---|
| 总览 | SparkX、DeepSeek、飞书发送、店铺数的状态；三条通知的开关与最近一次运行结果；最近发送记录 |
| 店铺与运营 | SparkX Token 授权的店铺列表（按 Profile，含站点和 Seller/Vendor）。可关闭某店铺的监控、为每家店指定运营（通知群成员，最多 10 位）。「采用全部建议运营」按店铺名把旧版（领星时期）店铺映射里的运营带过来，只采用仍在通知群的人 |
| 通知设置 | 通知群更换；总负责人、广告负责人；三条通知的开关、发送对象、DeepSeek 文字开关、实时监控时段与阈值；每条通知的「预览」 |
| SparkX 连接 | 保存或清除 MCP Token（只写不读）、测试连接并列出授权店铺 |
| DeepSeek | 保存 API Key（只写不读）、模型、超时；测试连接只调用免费的模型列表和余额接口 |
| 发送记录 | 最近 30 条通知和问答回复的状态 |

## 首次配置顺序

1. 「SparkX 连接」保存 Token →「测试连接」。
2. 「店铺与运营」→「从 SparkX 刷新店铺」→ 关闭不需要监控的店铺 → 为店铺指定运营（可先点「采用全部建议运营」再逐个核对）。
3. 「通知设置」确认通知群，选择总负责人和广告负责人。
4. 「DeepSeek」保存 Key 并测试（可选；未配置时文字由系统规则生成）。
5. 「通知设置」逐个「预览」→ 开启需要的通知 →「保存通知设置」。
6. 服务器 `.env` 中 `APP_DRY_RUN=false`、`FEISHU_ENABLED=true`、`SCHEDULER_ENABLED=true` 后才会真正发送。

预览会实时读取 SparkX（只读），不会排队或发送消息；每分钟最多 3 次。

## 文件

| 文件 | 内容 |
|---|---|
| `data/secrets/sparkx.json` | SparkX Token（0600）。文件损坏或权限不安全时停用 SparkX，不回退到其他凭据 |
| `data/secrets/deepseek.json` | DeepSeek Key、模型、超时（0600） |
| `data/sparkx-stores.json` | 每个 Profile 的监控开关和运营（0600） |
| `data/jx-notifications.json` | 三条通知的开关、发送对象和阈值（0600） |
| `data/feishu-routing.json` | 通知群、总负责人（`company_executive_digest`）、广告负责人（`ad_alert_subscribers`）。每次修改前在同目录留存完整备份 |
| `data/secrets/console-sessions.sqlite3` | 管理台会话（只存令牌哈希） |

SparkX 授权店铺列表的快照保存在数据库 `sync_jobs`（`job_type=sparkx_roster`），SparkX 暂时不可用时管理台仍能显示店铺。
