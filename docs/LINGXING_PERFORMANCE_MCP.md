# 领星店铺绩效与业绩通知接入

## 当前实现方式

店铺绩效和业绩通知的三个只读采集器已经接入 JX Alert 每日流水线，当前使用领星 OpenAPI，不依赖领星 MCP URL 或 X-Mcp-Key。

| 功能 | Method / Path | 请求参数 |
|---|---|---|
| 店铺绩效列表 | POST /basicOpen/customerService/storeTarget/list | sids、offset、length |
| 店铺绩效详情 | POST /basicOpen/customerService/storeTarget/detail | sid、pullDate |
| 业绩通知列表 | POST /basicOpen/customerService/performanceNotice/list | sid、offset、length |

列表和通知均按 offset/length 分页；通知采集不附加状态、已读或日期过滤，避免过滤条件隐藏待处理通知。绩效详情使用列表返回的最新 pull_date。

## 数据安全与判断规则

- 绩效列表 update_date 和通知响应 data.lastUpdateDate 是数据源更新时间，不使用 HTTP 请求时间代替。
- 这两个时间字段没有显式时区。只有确认领星数据源的实际时区后，才设置 LINGXING_PERFORMANCE_SOURCE_TIMEZONE。
- 未配置时区、缺少更新时间、数据过期、时间异常或采集失败时，系统保留 NOT_CONFIGURED / MISSING / STALE / FUTURE / FAILED 状态，不报“绩效正常”。
- 通知状态 1=待处理，2=已处理，3=无需处理；状态 0 和未知值保持 UNKNOWN，不自动关闭待处理告警。
- 详情 data.standard 作为平台标准保存。只有实际值和平台标准均可解析时才进行指标比较；缺失值不会转成 0。
- 每次采集的原始响应同时保存到数据库 RawSnapshot，并归档到 RAW_DIR/YYYY/MM/DD，使用原子写入和受限文件权限。
- 采集只读，不会修改 Amazon 店铺、绩效指标或广告活动。

## 启用前必须完成

默认 LINGXING_PERFORMANCE_ENABLED=false。请先通过领星当前账号文档和一份脱敏真实响应确认：
1. update_date / lastUpdateDate 的源时区。
2. 店铺绩效列表、详情、通知列表的实际字段名和分页总数字段。
3. AHR 状态、各绩效指标和 data.standard 的真实结构。
4. 店铺授权是否覆盖全部需要监控的 sid。

确认后，在 Linux 服务器 .env 中设置：

```env
LINGXING_PERFORMANCE_ENABLED=true
LINGXING_PERFORMANCE_SOURCE_TIMEZONE=Asia/Shanghai
LINGXING_PERFORMANCE_MAX_AGE_HOURS=8
LINGXING_PERFORMANCE_PAGE_SIZE=200
LINGXING_PERFORMANCE_MAX_PAGES=1000
```

上面的时区仅为格式示例，必须替换为你确认的真实源时区，不能直接照抄。

重建并重启 scheduler 后，查看采集日志、数据库 performance_sync_states、performance_notices、store_performance 和 RAW_DIR 归档。首次真实联调建议只启用一个测试店铺，并检查飞书是否收到待处理通知且不会重复刷屏。

## 可选的领星 MCP

仓库保留 scripts/inspect_lingxing_mcp_tools.py 和 LINGXING_MCP_URL / LINGXING_MCP_KEY 配置，用于将来探测其他尚未覆盖的领星 MCP 工具。它们不是上述三个 OpenAPI 采集器的运行前置条件。不要把 MCP Key、AppSecret 或 Token 写入 Git、日志、Excel 或原始业务数据快照。
