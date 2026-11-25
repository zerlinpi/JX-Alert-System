# JX Alert System

亚马逊广告监控：**SparkX MCP** 是唯一数据源（默认只读）；**DeepSeek** 像 WorkBuddy、Claude 等 Agent 一样直接调用 SparkX MCP，按 SparkX 官方 Skills 查询、分析并撰写通知，也在飞书里自由聊天；所有者打开「广告写入」后，负责人可以在飞书里确认修改广告；**飞书自建应用**负责发送；所有者在 **管理台** 配置一切。

| 通知 | 时间（北京） | 接收人 | 内容 |
|---|---|---|---|
| 广告日报 | 每天 08:30 | 通知群（@ 须知点名店铺的运营）+ 总负责人（私聊，可多位） | 美西最近完整日：全公司总销售额与广告销售额、总订单与广告订单、总销量与广告销量、广告花费、ACOS、TACOS、CVR、PPC 等核心指标与环比；有花费店铺明细（同样区分广告与总数，运营在最后一列）；须知 |
| 广告异常复盘 | 每天 08:30（可选仅周一） | 通知群（@ 有问题店铺的运营）+ 广告负责人（私聊，可多位）；可选私聊运营 | 近 7 日对比前 7 日，按主推产品的历史数据和客单价衡量：最严重、异常店铺总览、按类型的 campaign（附主推产品、客单价、产品历史、建议 CPC）、建议动作 |
| 实时监控 | 每小时（发送时段内） | 通知群（@ 对应运营）+ 广告负责人（私聊，可多位）；可选私聊运营 | 美西当日截至目前：高花费无转化、高花费低转化、低花费无转化、低花费高转化；同一活动同类提醒每天一次 |
| 飞书机器人 | 随时 | 群内 @机器人 或私聊 | 收到问题先在消息上点「OK」表示正在回答；自由聊天，需要数据时 DeepSeek 调用 SparkX 只读工具查询，记住同一段对话最近 6 轮；「广告写入」开启后，负责人回复「确认执行」才修改广告 |

三条通知的每条消息后面都附带同内容的 Excel 文件（文件名带日期），发给管理台为这条消息选的同一批接收人，并保存在服务器 `data/reports/`（管理台可下载）。三条通知默认全部开启，SparkX Token 和 DeepSeek Key 都保存后开始运行。店铺还没有指定运营时照常发到通知群（标注「未映射运营」，不 @ 人）；指定运营后，群消息顶部写「对应运营：@某某（店铺）」，只 @ 这次涉及店铺的运营。表格里的「运营」列固定放在最后一列。管理台里每条通知都可以先「预览」，再把这份预览「测试发送」到通知群或负责人（与正式消息一样，不带「测试」字样；不重新调用 DeepSeek，不计入定时发送）。默认模型为 `deepseek-flash`。

## DeepSeek 如何使用 SparkX

- 工具：三条通知、预览和测试发送只拿到 SparkX 标记为只读（`readOnlyHint`）且在白名单内的工具（`get_user_authorized_context`、`get_ads_perf`、`get_entity_metadata`、`get_operation_log` 等），工具名称、说明和参数结构与 SparkX 原样一致。
- 写入（所有者要求，2026-11-26）：管理台「广告写入」默认关闭。打开后，只有总负责人和广告负责人在飞书里提出修改时，DeepSeek 才拿到白名单内的写入工具（`batch_update_ads`、`create_*` 等）和全部写入 Skill：先列出修改计划（这一轮写入调用全部被拦截），同一位负责人 30 分钟内回复「确认执行」后才执行；每次写入调用都记入管理台「写入记录」。SparkX 的 Skill 平台工具永远不提供。
- 结果：SparkX 的返回原样交给 DeepSeek（含 `meta.hint`）；只有超过 15 万字符的结果会被截断并提示缩小查询。
- Skills：管理台从 GitHub `SparkXAI/SparkXAI-MCP` 安装官方清单里的只读和写入 Skills 到 `data/sparkx-skills/`（不进入本仓库）。只读 Skill 可逐个勾选是否使用（基础查询 Skill 始终启用）；写入 Skill 随「广告写入」开关一起启用。DeepSeek 通过 `read_skill` 按需阅读 SKILL.md 和参考文件，与 Claude、Codex 使用 Skills 的方式相同。当前 Token 没有提供某个 Skill 需要的工具时（例如市场数据），DeepSeek 看不到这个 Skill。
- 优先级：SparkX 返回的数据和官方 Skill 的要求第一，JX 的任务说明和输出格式第二，冲突时以 SparkX 为准；正文从简，Skill 要求说明的事项只用一句话。
- 任务：每条通知的「任务说明」可在管理台修改；统计日期、店铺范围和输出格式由系统附加。实时监控返回结构化结果，系统核对每个活动 ID 确实出现在 SparkX 结果中、并符合阈值，再按「每个活动每类每天一次」去重。

## 目录

```
src/jx_alert/
  ai/           agent（DeepSeek ↔ SparkX MCP 工具循环）、deepseek（Chat Completions，支持深度思考）、deepseek_config
  sparkx/       client（MCP 会话、只读与写入工具白名单、店铺列表）、skills（官方 Skills 安装与读取）、write_mode（「广告写入」开关）、
                stores（店铺目录）、credentials
  reports/      tasks（每条通知的任务与校验）、lanes（排队与发送前核对）、cards（Markdown → 飞书卡片）、settings、recipients、
                coverage / figures（日报缺数据名单与环比核对）、workbook / xlsx（Excel 副本）、archive（Excel 存档）
  notifications/ feishu_app、outbox、worker、routing、cards
  bot_service.py / bot_agent.py / feishu_bot.py   飞书机器人
  admin_console.py / admin_feishu.py / admin_ui/    管理台
  scheduler.py / api.py / config.py / storage/
```

详见：[通知内容与规则](docs/NOTIFICATIONS.md) · [管理台](docs/CONSOLE.md) · [飞书机器人](docs/FEISHU_BOT.md) · [部署、迁移与回滚](docs/DEPLOY.md)。

## 本地测试

```sh
pip install -e ".[dev,bot]"
ruff check . && pytest -q
node --test tests/ui/*.test.cjs
npm install --no-save jsdom@24 && node tests/ui/console.smoke.cjs
```

测试全部使用合成数据和模拟的 SparkX / DeepSeek / 飞书，不访问真实服务。

## 分支

仓库只保留 `main` 一个分支：每次更新测试通过后提交到 `main` 并推送，服务器只部署 `main` 上已核对的提交（见 [部署](docs/DEPLOY.md)）。
