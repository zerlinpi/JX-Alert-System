# 管理台

地址：`CONSOLE_PUBLIC_URL` 的 `/console/`（HTTPS）。用所有者口令登录（沿用原 `mcp-owner.json`，口令不变），会话 30 分钟。所有写操作都需要同源请求和页面令牌。

## 页面（左侧导航）

| 页面 | 作用 |
|---|---|
| 概览 | 「开始使用」清单（未完成的步骤直接跳到对应页面）；SparkX、DeepSeek、飞书发送、店铺状态；三条通知的开关与最近一次运行；最近发送 |
| 店铺与运营 | SparkX Token 授权的店铺。搜索；按「未指定运营」「未纳入通知」筛选；勾选多家店铺后批量指定运营、纳入或暂停通知；每行可单独编辑运营（通知群成员，最多 10 位） |
| 通知与报告 | 通知群和负责人；三条通知各一张卡片：开关、发送对象、频率/时段、提醒阈值、DeepSeek 任务说明（可改写、可恢复默认）。修改后底部出现「有未保存的修改」，可撤销或保存。「预览」用当前（含未保存的）设置在后台让 DeepSeek 查询 SparkX 并生成卡片，不会排队或发送 |
| SparkX MCP | 保存或清除 MCP Token（只写不读）、测试连接并列出授权店铺；安装 / 更新 / 检查官方 Skills；查看提供给 DeepSeek 的只读工具 |
| DeepSeek 与机器人 | API Key（只写不读）、模型、单次请求超时、深度思考开关；测试连接只调用免费的模型列表和余额接口；飞书机器人使用说明 |
| 发送记录 | 最近 30 条通知和问答回复的状态 |

## 首次配置顺序

1. 「SparkX MCP」保存 Token →「测试连接」→「安装 / 更新」官方 Skills。
2. 「DeepSeek 与机器人」保存 Key 并测试。
3. 「店铺与运营」→「从 SparkX 刷新店铺」。Token 授权了很多店铺时，用搜索（例如「加拿大」）+ 全选 →「暂停通知」只保留需要的站点；再为店铺指定运营（可以稍后再指定）。
4. 「通知与报告」确认通知群，选择总负责人和广告负责人。
5. 逐个「预览」核对内容（每次约 1–10 分钟，会消耗 DeepSeek tokens），需要时改写任务说明后再预览、保存。三条通知默认全部开启：实时监控在发送时段内每小时运行，日报和复盘在 08:30–10:00 生成。
6. 服务器 `.env` / 运行配置中 `APP_DRY_RUN=false`、`FEISHU_ENABLED=true`、`SCHEDULER_ENABLED=true` 后才会真正发送。

店铺不必全部指定运营才开始使用：未指定运营的店铺照常发到通知群（标注「未映射运营」）；指定之后，群消息会 @ 出问题店铺的对应运营。

## 文件

| 文件 | 内容 |
|---|---|
| `data/secrets/sparkx.json` | SparkX Token（0600）。文件损坏或权限不安全时停用 SparkX，不回退到其他凭据 |
| `data/secrets/deepseek.json` | DeepSeek Key、模型、超时、深度思考开关（0600） |
| `data/sparkx-skills/` | 官方只读 Skills（`installed.json` 记录版本与提交），从 GitHub 安装，不在代码仓库里 |
| `data/sparkx-stores.json` | 每个 Profile 是否纳入通知和对应运营（0600） |
| `data/jx-notifications.json` | 三条通知的开关、发送对象、阈值和任务说明（0600；任务说明为空表示使用默认） |
| `data/feishu-routing.json` | 通知群、总负责人（`company_executive_digest`）、广告负责人（`ad_alert_subscribers`）。每次修改前在同目录留存完整备份 |
| `data/secrets/console-sessions.sqlite3` | 管理台会话（只存令牌哈希） |

SparkX 授权店铺列表的快照保存在数据库 `sync_jobs`（`job_type=sparkx_roster`），SparkX 暂时不可用时管理台仍能显示店铺。
