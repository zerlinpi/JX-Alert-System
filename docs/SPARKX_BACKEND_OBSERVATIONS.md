# 运营台选择 SparkX：后台 SP 观测，不是投递切换

本功能不是 ChatGPT Agent 或桌面插件。运营台为指定店铺选择 SparkX 后，JX 小时巡检的后台进程通过固定 SparkX MCP HTTPS 端点调用 `get_user_authorized_context` 与 `get_ads_perf`。只使用有明确授权的只读 Token 和单个 SparkX profile；不改活动、预算、竞价、商品或供应端配置。默认仍使用领星。凭据轮换须按运行环境的安全流程重新载入进程，旧验证不跨 Token 使用。

- 运营台需要核对 JX SID、Amazon Marketplace ID、币种和店铺 IANA 统计时区，然后验证同一真实 SparkX profile。验证只查询该店最近一个已结束的当地统计日，必须返回可核验的非空完整查询，验证成功不写商品/广告报表或飞书记录。空集不表示零。
- 确认选择后，每轮小时巡检先保持原有领星销售、健康、日报流程，再以 SparkX 读取该店 SP 活动：最近一个完整当地日报日，以及本地当前统计日的小时流量。小时时间以 SparkX 返回的当地日期及小时分组呈现，不转换为精确 UTC 时点；到达上限、同日同活动与小时重复（含不可证明的夏令时重复小时）、跨页顺序异常、跨 profile、币种或日期不一致、缺口或分页不完整均不能成为合格观测。仅储存验证成功的分区，每轮同一分区不重复入库；不把日报和小时归因口径相加，不用其他来源补缺。
- 两类观测分别追加至 `RawSnapshot` 的 `sparkx_backend_campaign_daily` 与 `sparkx_backend_campaign_hourly` 源。各源与历史领星 `DailyMetric`、覆盖、Campaign、告警、飞书队列均分开；可回查保存的旧版本。请求开始时间用于解释这次采样边界，捕获时间记录 JX 收到结果之后，不是供应端实际更新时间。目前没有可靠的 SparkX 水位，保存的是观测而非覆盖认证，不把昨天转换称为最终数据。标准通用免责声明不当作错误；明确的部分结果、截断或不完整提示会阻断相应分区。其他未识别的供应端提示只记录“有提示”标记，不保存供应端自由文本。店铺类型已明确时，日转化字段按 Seller SP 7 天／Vendor SP 14 天标注归因窗；如果授权资料没返回类型，保留独立原始观测但标为归因窗未核实，不默认 Seller，也不判定成熟。小时仅收曝光、点击和花费，不读小时 AI 占位字段。
- 选定 SparkX（即使授权失效）时该店不使用领星 SP 小时报表、主活动或日内替代观测；本地授权绑定失效时显示暂停；供应端请求失败记录在小时任务，运营台即使仍显示历史成功观测也不代表当前连接正常。不会自动切回。领星库存接口暂不可用时，已在仓库且仍在明确授权范围、采集开关开启的店铺可继续 SparkX 观测。每一页实际请求前重新检查相同绑定。改选或撤销时，正在获取的回复再次核验选择与范围；最终写入与运营台改选串行，不迁移旧来源的结果。每个统计日和粒度独立查最近一次观测。切回领星只由管理员手动完成；它不会读取 SparkX 的旧行。
- 当前只支持 **SP 活动级观测**，不承诺 SparkX 自动异常、SB/SD、全公司经营指标、飞书主动通知或历史对账切换。每日领星独立会计/销量、健康及其他既有渠道的口径与授权不变。切换前先在非生产环境用批准的一个店验证真实覆盖、数据及读取权限；在需要保留原告警时不要认为这条观测通道已经提供同等告警。

供应端依据：[SparkX 活动查询、日期和分页](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/SKILL.md)、[字段、小时口径与归因窗](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/references/field-reference.md)、[授权账户与币种规则](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/references/platform-notes.md)。
