# SP 历史采集与对账验证

`scripts/validate_sp_history.py` 用于单店铺、单活动、指定日期范围的只读验证。它将采集证据写入独立 SQLite 文件，保留每次采集版本，供排查日报缺失、小时数据差异和历史数据更新。它尚未自动接入调度器，也不会更新生产 `DailyMetric`。

## 数组签名的官方依据

[领星官方接入指南](https://apidoc.lingxing.com/new/guides/new-instructions/)链接的 [Python SDK](https://apidoc.lingxing.com/file/openapi-python3-sdk-20230419.zip) 中，`sign.py` 使用 `orjson.dumps(v, option=orjson.OPT_SORT_KEYS).decode()` 处理列表和字典；注释要求使用标准 `json` 时去掉分隔空格。列表 `[1123, 22555]` 因而以紧凑 JSON 文本 `[1123,22555]` 参与签名，保留列表顺序。[官方 Java SDK](https://apidoc.lingxing.com/file/openapi-sdk-java-20240730.zip) 的 `OrderListDemo.java` 也先将列表序列化用于签名，再恢复请求体中的列表类型。

本次实现只允许下列两个路径的 `campaign_ids` 使用该规范化方式：

- `/pb/openapi/newad/spAdvertiseHourData`
- `/pb/openapi/newad/spTargetHourData`

规范化仅作用于签名参数副本，实际 JSON 请求体仍传数组。数组元素必须为正整数，不接受布尔值；与 `campaign_id` 合并去重后最多 50 个 ID，广告商品端点还限制原数组最多 50 项。其余复杂参数继续拒绝签名，不对其他端点作推定。

[投放小时文档](https://apidoc.lingxing.com/new/api-docs/newAd/report/spTargetHourData/)明确说明 `campaign_id` 与 `campaign_ids` 至少传一个；部分其他小时页面将两者都标为必填，描述存在差异。验证服务默认仅传已实测可用的单个 `campaign_id`，不需要数组即可完成单活动对账。

## 采集范围

每次运行先完整获取 `/pb/openapi/newad/spCampaigns` 活动基础数据，按 `campaign_id` 联结名称、状态和起止日期。每日获取 [SP 活动日报](https://apidoc.lingxing.com/new/api-docs/newAd/report/spCampaignReports/)，随后独立采集四个小时维度：

| 维度 | 只读端点 | 额外参数 | 小时实体键 |
| --- | --- | --- | --- |
| 活动 | [`spCampaignHourData`](https://apidoc.lingxing.com/new/api-docs/newAd/report/spCampaignHourData/) | 无 | 活动、小时 |
| 广告组 | [`spAdGroupHourData`](https://apidoc.lingxing.com/new/api-docs/newAd/report/spAdGroupHourData/) | 无 | `group_id`、小时 |
| 广告商品 | [`spAdvertiseHourData`](https://apidoc.lingxing.com/new/api-docs/newAd/report/spAdvertiseHourData/) | `agg_dimension=ad` | `ad_id`、小时 |
| 投放 | [`spTargetHourData`](https://apidoc.lingxing.com/new/api-docs/newAd/report/spTargetHourData/) | `agg_dimension=target` | `targeting_id`、小时 |

所有请求使用广告接口版本 2。日报不带未列入官方参数的 `campaign_id` 过滤；它按 `sid` 获取完整报告，再在本地匹配目标活动。初始请求使用 `offset=0,length=100`，后续使用 `next_token`。总条数跨页变化、缺失或重复游标、重复活动记录都会使该次日报验证失败，避免将首页未匹配误当作完整结果。

小时响应必须满足声明的总条数、日期、活动、店铺广告 `profile_id` 及实体键校验，指标必须存在且为有限数值。默认请求间隔为 1.1 秒；服务函数可通过 `request_interval` 调整，测试使用 0。

## 手动运行

在已安装项目依赖的开发工作区运行。默认只显示计划，不读取设置、不获取令牌、不发 API 请求，也不创建数据库或输出文件：

```bash
PYTHONPATH=src python scripts/validate_sp_history.py \
  --sid 5174 \
  --campaign-id 141469378857743 \
  --start 2026-11-05 \
  --end 2026-11-11 \
  --output-dir ./work/sp-validation
```

显式加 `--collect` 才执行采集，并通过现有配置获取凭据；不要把凭据放入命令参数或打印配置内容：

```bash
PYTHONPATH=src python scripts/validate_sp_history.py \
  --collect \
  --sid 5174 \
  --campaign-id 141469378857743 \
  --start 2026-11-05 \
  --end 2026-11-11 \
  --output-dir ./work/sp-validation
```

起止日期均包含在内，范围限 1–60 天；各端点实际允许的历史范围以官方文档和接口响应为准。相同输出目录中会追加使用 `sp-validation.sqlite`，并为每次运行创建新的 `sp-validation-<随机标识>.json` 摘要。CLI 只在此独立 SQLite 中创建 `RawSnapshot` 表，不调用生产数据库初始化、迁移或日指标写入流程。

## 状态与对账解释

| 状态 | 含义 | `metrics` |
| --- | --- | --- |
| `NO_RECORDS` | 请求成功且完整结果为空 | `null` |
| `UNMATCHED` | 完整日报有记录，但没有目标活动 | `null` |
| `ZERO_SPEND` | 已匹配记录明确返回花费为 0 | 保留实际指标 |
| `MATCHED` | 已匹配记录，且明确返回非零花费 | 保留实际指标 |
| `REQUEST_FAILED` | 客户端或上游请求失败 | `null` |
| `INVALID_RESPONSE` | 分页、标识、日期、指标或响应结构校验失败 | `null` |

`ZERO_SPEND` 只证明该响应中花费为 0，不意味着订单、销售额等所有指标均为 0。无记录、未匹配和失败均不会补成零花费。

日报摘要的 `records` 保留全量非敏感活动标识、活动名称和指标，支持检查目标活动是否位于后续页面。仅当日报和相应小时维度均有可用目标指标且 `profile_id` 一致时，才计算 `cost`、`clicks`、`impressions`、`orders`、`sales` 的差值：

```text
delta_hour_minus_daily = 该小时维度的目标活动汇总 − 日报目标活动指标
```

汇总和差值使用 `Decimal`，在 JSON 中输出十进制字符串。每个小时维度单独与日报比较，结果为 `EQUAL` 或 `DIFFERENT`；数据不可用时为 `UNAVAILABLE`，店铺广告范围不一致时为 `SCOPE_MISMATCH`，均不输出数值差。四个小时维度不得相加，差异本身也不能证明接口归因口径相同或历史数据已经最终定稿。

运行级 `COMPLETE` 仅表示请求和契约校验完成，可以包含无记录、未匹配或指标差异；`PARTIAL` 表示至少一个采集范围请求或校验失败。CLI 对 `PARTIAL` 返回非零退出码。

## SP 告警恢复隔离

SP 活动指标缺失时，不会把缺失项补成零。其他活动若其所需指标均完整，仍可独立评估和恢复既有告警；恢复范围通过 `recovery_entity_ids` 限定。指标缺失的活动保留原告警状态。空的恢复实体集合不会恢复任何活动；未提供该参数的其他监控维度继续使用原有完整范围恢复语义。

## 证据、回补与现有限制

每次运行生成独立 `run_id`。各响应页在业务校验前追加到 `RawSnapshot`，包含采集时间、安全业务请求参数、端点、响应及其中的请求链路 ID；敏感字段和已知客户端凭据会脱敏。每天的验证结果、失败状态和整次运行摘要也分别追加保存。重复采集同一日期会保留新旧版本，不替换旧快照或既有 JSON 摘要，因此可以观察历史指标变化，而不把旧版本抹掉。

直接调用 `collect_sp_validation(client, db, ...)` 时，调用方负责提交事务，包括部分失败运行的证据。服务仅向提供的会话添加并刷新快照，不自行 `commit()`；CLI 在采集后执行 `db.commit()`。调用方回滚会撤销尚未提交的证据。

当前 `LingxingClient` 对失败请求抛出异常，不向采集服务暴露失败响应体。服务只保存异常类别和固定失败码，不保存异常消息或堆栈，避免将签名 URL 或凭据写入证据。成功返回但业务校验失败的原响应页仍有快照。

本工具尚未自动纳入日常调度、MariaDB 生产持久化或生产告警。后续应先审查真实采集证据和接口归因差异，再验证 MariaDB 事务、并发与失败恢复后整合；现有生产 `.env`、数据库、Docker 服务和广告设置不由此工具改写。实际执行结果和测试数量以本次交付报告为准。
