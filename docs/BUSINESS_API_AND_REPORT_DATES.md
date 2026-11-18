# 经营接口及日报日期

日报于北京时间08:00推送，标题按北京时间的前一日命名。例：北京时间11月12日发送「11月11日日报」，实际采用美国11月10日的完整数据，正文简注「美国统计日：2026-11-10」。查询、事实、Excel、广告成熟度及去重仍使用美国统计日，不把改标题当作修改数据日期。历史回退会保留实际统计日期，不冒充目标日。

## 两类接口证据

- 产品表现：`POST /bd/productPerformance/openApi/asinList`，单店字符串sid、单日、ASIN、USD、全部商品。直接保存销量、净销售额、广告花费等原值，广告费/净销售额仅在同一来源分子分母完整且分母大于零时计算。
- 利润报表：`POST /bd/profit/report/open/report/seller/list`，单店sids数组、单个结算日、USD、已发放交易。直接保存平台收入、毛利润、毛利率及结算退款率；不把结算销量冒充下单销量，也不将结算退款率冒充MCP看板的退款费用比例。

两者分别保存为`api_business_product`、`api_business_finance`，原始响应保留版本。MCP经营看板维度及现有付款、广告事实保持独立，不跨来源、跨口径补值或计算环比。经营看板可用时优先展示其七项；缺少看板时可展示标有下单/结算口径的接口经营日报。未确认字段不占空行，源失败不解释为真实零或广告正常。

接口财务sids签名采用官方Python SDK `SignBase.format_params`的紧凑JSON，仅放行这一明确接口的单店数组；其余复杂参数仍拒绝。实际请求体保留JSON数组，不将其改成签名字符串。

## 只补经营数据

在服务器项目目录执行以下命令，可补采一个已结束美国统计日及其前一日对比，并保存原始JSON和Excel，不触发飞书通知：

```sh
sudo docker compose -p jx-alert exec -T app python /app/scripts/backfill_business.py --report-date 2026-11-16 --source both
```

仅接口使用`--source api`，仅经营看板使用`--source mcp`。可追加`--store-id 5174`限制店铺。默认只处理既有范围内已授权且采集开关开启的店铺；停用店铺不会被重新启用。返回记录表示实际采集结果，不能仅因凭据已保存就宣称补齐。

源文件及每日全历史Excel保留这两个新维度；补采按同店同日替换当前事实，保留原始版本，不重复累计金额。日报仍使用既有08:00队列与去重，不因补采重发历史消息。

自动双源采集中的SP校验、历史销量接口失败，不再阻断独立的MCP经营采集。原步骤记录脱敏失败，整店保留部分失败状态及重试，不能因经营看板成功就标记全店采集完成。暂停开关、配置错误和纯接口模式保持原有保护。

经营数据已保存但Excel归档待重试时，补采返回`COMPLETE_WITH_ARCHIVE_RETRY`，任务记为`SUCCEEDED_WITH_WARNINGS`，命令退出码为2；不得把数据库成功冒充文件归档成功。

官方依据：[产品表现](https://apidoc.lingxing.com/new/api-docs/Statistics/AsinListNew/)、[店铺利润报表](https://apidoc.lingxing.com/new/api-docs/Finance/bdSeller/)、[看板口径](https://www.lingxing.com/help/article/manageBoard)、[官方Python SDK](https://apidoc.lingxing.com/file/openapi-python3-sdk-20230419.zip)。
