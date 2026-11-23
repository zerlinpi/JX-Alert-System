# JX Alert System

亚马逊广告监控：**SparkX MCP** 是唯一数据源（只读），**DeepSeek** 负责撰写说明和回答问题，**飞书自建应用**负责发送，所有者在 **管理台** 配置一切。领星数据源已移除。

| 通知 | 时间（北京） | 接收人 | 内容 |
|---|---|---|---|
| 广告日报 | 每天 08:30 | 总负责人（私聊），可选通知群 | 昨日（美西完整日）全公司店铺总销售额、总订单量、广告花费、广告销售额、ACOS、TACOS、CVR、PPC、ROAS 及环比；站点汇总表；有花费店铺明细（含对应运营）；「须知」 |
| 广告异常复盘 | 每天 08:30（可选仅周一） | 广告负责人（全部店铺，标注运营）、对应运营（只含其店铺），可选通知群 | 近 7 日对比前 7 日：一、最严重（全店零曝光等）；二、异常店铺总览；三、按类型看 campaign（①高花费零出单 ②ACOS 明显恶化 ③加预算后销售反而下滑 ④异常停摆，附建议 CPC）；四、建议动作 |
| 实时监控 | 每小时（发送时段内） | 广告负责人、对应运营，可选通知群 | 美西当日截至目前：高花费无转化、高花费低转化、低花费无转化、低花费高转化；同一活动同类提醒每天一次 |
| 飞书问答 | 随时 | 群内 @机器人 或私聊 | DeepSeek 直接调用 SparkX 只读工具查询并回答 |

## 数据与安全规则

- 只调用 SparkX 只读工具 `get_user_authorized_context`、`get_ads_perf`、`get_entity_metadata`；写工具（`batch_update_ads`、`create_*` 等）在代码中不可达。
- 表格里的每个数字都由服务端从 SparkX 行计算；缺失显示「—」，从不当作 0。分页不完整、币种不一致、行重复或超出请求范围时整条通知不生成。
- DeepSeek 只改写服务端计算好的事实；回答中出现事实之外的数字、日期或店铺时会被要求重写一次，仍不符合就改用系统规则文字（问答则直接展示 SparkX 原始结果）。
- 店铺报表时区按 `America/Los_Angeles`；日报用已结束的完整日，近 7–14 天的销售和订单仍会回补，卡片中注明。
- 卡片在排队时冻结；发送前重新核对开关、发送时段和接收人。可能已提交飞书但未确认的消息不自动重发。
- 密钥（SparkX Token、DeepSeek Key、飞书 App Secret）只写不读，保存在 0600 私有文件。

## 目录

```
src/jx_alert/
  sparkx/       client（只读 MCP 会话与校验）、collect（三条通知的数据采集）、analysis（确定性计算）、
                stores（店铺目录：监控开关与对应运营）、credentials（Token）
  reports/      settings（通知开关/阈值）、recipients（接收人）、cards（飞书卡片）、lanes（排队与发送前核对）
  ai/           deepseek（Chat Completions 客户端）、deepseek_config、narrative（文字生成与数字核对）
  notifications/ feishu_app、outbox、worker、routing、cards
  bot_service.py / bot_agent.py / feishu_bot.py   飞书问答
  admin_console.py / admin_feishu.py / admin_ui/    管理台
  scheduler.py / api.py / config.py / storage/
```

详见：[通知内容与规则](docs/NOTIFICATIONS.md) · [管理台](docs/CONSOLE.md) · [飞书问答](docs/FEISHU_BOT.md) · [部署、迁移与回滚](docs/DEPLOY.md)。

## 本地测试

```sh
pip install -e ".[dev,bot]"
ruff check . && pytest -q
node --test tests/ui/*.test.cjs
npm install --no-save jsdom@24 && node tests/ui/console.smoke.cjs
```

测试全部使用合成数据和模拟的 SparkX / DeepSeek / 飞书，不访问真实服务。
