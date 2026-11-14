# 每日全维度 Excel 快照

每天北京时间 **23:30** 保存一套数据库已有全部业务历史，保留各自源报表日期、归因日期、采集时间及SP版本。18:30的采集与08:00的日报提醒独立运行；Excel归档不会调用领星补造数据，也不会阻塞Chat MCP。

## 保存位置

- 宿主机：`/opt/jx-alert-system/data/excel/daily/YYYY/MM/DD/`
- 容器内：`/app/data/excel/daily/YYYY/MM/DD/`
- `manifest.json`列出快照时间、每个工作簿的中文维度、行数、文件指纹及空维度状态。
- 中文归档目录工作簿便于在Excel中查找对应数据文件。

通过WinSCP/SFTP登录已有服务器后下载当天整个目录。无需公开下载网址，也不将业务报表推送到GitHub。

## 数据维度

| 内容 | 保留范围 |
| --- | --- |
| 店铺、商品、广告活动、广告商品 | 已保存主数据、状态、ID、币种及时区 |
| 店铺、ASIN、MSKU销量 | 全历史标准化指标，包含全部已入库指标名称 |
| SP/SB/SD活动及SP/SD商品广告 | 全历史花费、订单、销量、销售额、点击、曝光及其他已入库指标 |
| SB广告创意、投放、归因购买商品 | 分工作簿保存源报表记录及各自归因指标 |
| SD商品定位、已购买商品 | 分工作簿保存源报表记录及各自归因指标 |
| SP日报、小时、广告商品、投放 | 所有已保存run_id版本，保留小时与对象，不把版本相加 |
| 对账、采集覆盖、采集任务 | 原始日/小时指标、差额、匹配数、采集状态及版本 |
| 绩效 | 历史健康评分源观测、七项指标、标准、通知索引及数据质量状态 |
| 广告七项指标 | 各店有采集证据的最近已结束报表日；逐项保留实际归因日期和参考日期 |
| 异常与通知 | 已保存异常状态及通知投递状态，不导出收件人身份 |
| 原始响应 | Excel保存来源索引；原JSON正文继续留在私有数据库和原始归档目录 |

归档目录用“无已保存记录”标记空工作簿。查阅“采集覆盖”工作簿可以区分成功返回空记录、采集失败和未采集。SB、SD 明细与活动报表使用各自粒度，店铺广告汇总沿用活动报表，避免重算同一笔花费。SP 小时、广告商品和投放明细沿用明确配置的活动范围。健康源时区未知时，绩效数据仍用于观测归档。

SB 明细来源于领星的[广告创意报告](https://apidoc.lingxing.com/new/api-docs/newAd/report/listHsaProductAdReport/)、[广告投放报告](https://apidoc.lingxing.com/new/api-docs/newAd/report/listHsaTargetingReport/)和[归因购买报告](https://apidoc.lingxing.com/new/api-docs/newAd/report/hsaPurchasedAsinReports/)。SD 明细来源于[商品定位报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/sdTargetReports/)和[已购买商品报表](https://apidoc.lingxing.com/new/api-docs/newAd/report/sdAsinReports/)。

18:30 采集对已授权店铺拉取最近已结束报表日及其前 14 日对应的归因检查日。飞书问答显示明细接口状态与源记录数。如需补采其他日期，可在服务器运行以下命令，`--store` 和 `--date` 可重复指定，每次至多七个报表日。

```bash
sudo docker compose -p jx-alert exec -T scheduler python /app/scripts/collect_sb_sd_details.py --store 5174 --date 2026-11-13
```

## 完整性与重试

一次导出使用一致的数据库读快照。全部文件写完并校验后才发布完成目录；失败不留下伪完成清单，每五分钟重试。跨午夜的失败计划仍可恢复，实际截至时点以清单中的UTC时间为准，不能把补做快照当作过去时刻的数据库状态。

同日成功快照保持不变，重复执行会核对文件指纹后复用。升级前完成的快照保留原有工作簿，新建快照增加 SB、SD 明细工作簿。损坏或不完整目录保留后重建，不删除数据库、历史文件或Docker卷。超过Excel单表行数上限自动分表，避免截断。长ID保留文本、SP高精度金额保留精确十进制文本，真实0与缺失空白分开，外部文本不能成为Excel公式。

只导出白名单业务列，不导出`.env`、密钥、令牌、OAuth数据、客户身份或任意原始JSON。

## 手动补做

使用已经更新的调度服务，指定北京时间归档日期：

```bash
cd /opt/jx-alert-system
sudo docker compose -p jx-alert exec -T scheduler python /app/scripts/export_daily_snapshot.py --snapshot-date 2026-11-14
```

脚本只读取已有数据，不请求领星、不重建环境。同日完成时返回`ALREADY_COMPLETE`；未完成返回失败退出码，不把尝试成功当作归档完成。

运行配置支持`excel_daily_snapshot_enabled`和`excel_daily_snapshot_cron`；还需原有`excel_archive_enabled=true`。默认快照计划为北京时间`30 23 * * *`。调度代码来自镜像，因此部署此改动要构建并更新scheduler服务；只拉取Git或重启旧镜像不会加载新任务。可以保持原MCP服务运行。
