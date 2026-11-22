# 负责人经营日报

参考 [Grafana 表格](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/table/) 的概览与明细分层，以及 [飞书原生表格](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/table) 的组件限制：先发同币种汇总，再分页发店铺明细。用真实卡片生成代码离线预览虚构示例，不发飞书：

```sh
PYTHONPATH=src python scripts/preview_owner_daily.py
```

| 指标 | 口径 |
| --- | --- |
| 广告花费 | 领星[产品表现](https://apidoc.lingxing.com/new/api-docs/Statistics/AsinListNew/)的 `spend`。使用已验证的完整 ASIN 分区，包含源端支持的 SP、SB、SBV、SD 及共享分摊；不再加广告活动报表，避免重复。不是广告账单对账总额。 |
| 净销售额 | 同日期同店铺产品表现 `net_amount`；明确为净额，非结算收入。 |
| 件数退货率 | 观察窗口内的 `return_goods_count / volume`。按同一完整覆盖集合汇总分子分母后相除，不平均店铺百分比；非原订单批次最终退货率。零分母不报 0%；跨期退货可超过 100%。 |
| 销售退款额 | 领星[店铺利润报表](https://apidoc.lingxing.com/new/api-docs/Finance/bdSeller/)的 `totalSalesRefunds`，按结算日和已发放状态取扣款绝对值。不是实时退款承诺，不含其他费用退款，不从净额里再扣一次。 |

## 日期和缺口

卡片写实际的店铺统计日和生成时刻（不是供应端更新时间），不用北京时间的「今日」替换。未结束的店铺日期只能读该日期、距生成时不超过 90 分钟的已存分区，并标成暂估；不拿昨日数据顶替，也不启动额外采集。已结束日期只接受收盘后的同日分区。以同一统计日、同一币种出汇总；不做跨币种加总、不把空接口当成零，也不把部分覆盖叫全量。

现有早报仍按已结束的店铺当地统计日及北京时间 09:00 发送规则工作。试点开关 `FEISHU_OWNER_DAILY_REPORT_ENABLED` 默认关闭，只在验证数据后由部署人员在隔离环境启用。开启后在现有早报中把负责人汇总和分店铺卡置于前面，替代单店经营卡，不替代店铺健康或实质广告异常；不另开自动推送。没有同日报表或全范围无可核验指标时保留既有日报行为，不虚构负责人数据。要上线「今天截至某时」，仍需验证当前日产品表现/退款数据可用性，商定截止和投递时刻，再安排独立同日取数与投递，不要把早报日期改名。

## 收件范围及回滚

只沿用现有已确认的飞书应用通知群；发前复核当前群、店铺授权及通知开关。仅在该群内重算实际可 @ 的已映射负责人，不自动增加群、私发或重放。汇总页有店铺权限变化时取消该冻结页，不发送包含该店的总数。开关关闭不影响其他早报或历史，回滚不删除持久化数据。SparkX 试点与本卡数据源独立。
