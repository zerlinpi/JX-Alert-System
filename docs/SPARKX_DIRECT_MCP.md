# SparkXAI MCP 原样转发（隔离试点）

JX 可以为原生 SparkXAI 广告及支持的 ASIN 经营查询提供一个独立、只读的 MCP 入口。它使用官方 Streamable HTTP 地址，不需要安装桌面 Plugin，也不运行 SparkX 的读写 Skills。JX 不做二次 AI 分析、不换算指标、不补零、不混入领星数据，也不重新排版 MCP 的文本或结构化结果。

| JX 功能 | 本次覆盖 |
| --- | --- |
| 手动 MCP 广告/ASIN 指标 | 转发 SparkX 原生 `get_ads_perf`，保留原生结果、字段名与分页 |
| SP / SB / SD | 由 SparkX 查询、授权和数据覆盖决定；返回空结果不代表业务为零 |
| 账户健康、违规通知、完整店铺/MSKU/结算口径 | 无已公开、可等价替代的 SparkX 接口，不切换 |
| 飞书日报、每小时告警、机器人和 Excel | 未切换。公开周报/月报 Skills 由 AI 组织数据并排版，不是供应端成品报告 |
| 广告、预算、竞价、托管组写入 | 不支持；中转不给客户端暴露任何写工具 |

官方参考：[安装与权限](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/docs/installation.md)、[查询字段](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/SKILL.md)、[周报编排](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-weekly-ads-report/SKILL.md)。

## 验证与启用

1. 在 SparkX 生成**仅允许选定 Amazon Ads profile 和广告效果只读范围**的 MCP Token（`amazon_sa_performance_data:read`），在部署端私下设置 `SPARKX_AI_TOKEN`。不要把 Token 发到聊天、写入 Git、放进请求正文或日志。安装说明的桌面 Plugin 同时包含写功能，不要在 JX 服务端安装它。
2. 设置 `SPARKX_PROFILE_IDS` 为审核过的 SparkX 广告 profile ID 逗号列表，不是 JX 店铺 SID。每次查询只能选其中一个 profile，服务端仍会再次校验授权；需由管理员先核对广告账户、站点、币种和角色。未审核访问者不得拿到中转入口。
3. 先运行 `python -m jx_alert.sparkx_direct`。它通过官方 `get_user_authorized_context` 验证指定范围，只打印确认的数量，不打印账户目录或凭据。运行环境须能直连 `https://mcp.sparkx.cn/mcp`；中转不跟随重定向，也不读取系统代理配置。
4. 仅在隔离试点 MCP 进程设置 `MCP_OUTPUT_MODE=sparkx_direct`。HTTP 入口用 `MCP_AUTH_MODE=bearer`，另设 `SPARKX_RELAY_BEARER_TOKEN`（至少 32 字符，不得与 JX 原入站 Token 或 SparkX 上游 Token 相同），绑定回环或受控私有入口。不要替换现有生产 MCP、日报或 Scheduler。默认 `MCP_OUTPUT_MODE=jx` 保留现有行为。不要在多人共享进程运行 stdio 中转；stdio 的访问边界是启动进程所在的账号。
5. 用一个批准的测试 profile 比较直接调用 SparkX 和经 JX 中转后的原生 `content` / `structuredContent`，再验证未知 profile、写工具及领星/旧 JX 工具均不可用。不要把其他人、订单或凭据的信息放在 `userContext`。还没验证真实授权与数据覆盖前，不得关闭现有通知。

### Docker：独立试点，不替换原 MCP

使用仓库提供的 `docker-compose.sparkx.yml` **单独**启动试点，不要和主 Compose 文件合并，不要修改生产 `.env`。在仓库之外建立仅部署账户可读的环境文件（权限 0600），填入 `SPARKX_AI_TOKEN`、`SPARKX_RELAY_BEARER_TOKEN` 和 `SPARKX_PROFILE_IDS` 三项，再从试点检出目录运行：

```sh
export SPARKX_PILOT_ENV_FILE=/absolute/private/path/sparkx-pilot.env
docker compose -p jx-sparkx-pilot -f docker-compose.sparkx.yml build sparkx-mcp
docker compose -p jx-sparkx-pilot -f docker-compose.sparkx.yml run --rm sparkx-mcp python -m jx_alert.sparkx_direct
docker compose -p jx-sparkx-pilot -f docker-compose.sparkx.yml up -d sparkx-mcp
```

试点仅发布 `http://127.0.0.1:8011/mcp`，不依赖生产数据库、不挂载生产数据或凭据、不会重启旧 MCP。先确认 8011 空闲；不要把试点端口开放到公网。停用用同一文件和项目名执行 `stop sparkx-mcp`，不删除数据或更改原服务。

## 调用示例

```json
{
  "arguments": {
    "profileIds": [1234567890123456],
    "factEntity": "campaign",
    "dateStart": "2026-10-01",
    "dateEnd": "2026-10-07",
    "metrics": ["Impressions", "Clicks", "Spend", "Sales", "Conversions"],
    "select": ["date", "campaign.campaignType_"],
    "language": "zh",
    "userContext": "按日查看广告表现",
    "page": 1,
    "pageSize": 100
  }
}
```

这是指向中转工具 `get_ads_perf` 的 MCP 参数，不是候选 profile 的真实 ID。按小时查 `campaign` 可以选 `hour` 分组，也可以不选来聚合小时源；按天查普通实体可传空 `timeGranularity`，不由 JX 改写。实际数据日期、归因口径和空记录以供应端为准。传输异常直接报错，不回退领星。若要停用，停止独立中转进程或撤销专用凭据，现有数据卷和 JX 主服务不受影响。

## 飞书：手动发送一页 SparkX 原始报告（默认仅预览）

这是**独立的运维命令**，不挂在 MCP、Scheduler、机器人或运营台上。正常调用 `get_ads_perf` 依然只读，不会触发发信。默认运行下列命令会访问批准的 SparkX profile，输出 Feishu 2.0 卡片 JSON **到本机终端**，不入队、不发信。输入文件内容是上例内层的查询对象（不包含外层 `arguments`）。

```sh
python -m jx_alert.sparkx_feishu < /private/approved-query.json
```

- 同一次运行先从 `get_user_authorized_context` 核对单一 profile，再发起原查询。绝不把账户目录、凭据、请求上下文或响应 `_meta` 写入卡片。
- 有面向用户的原生报告文本时，**本地预览**以原文为主体，不另做摘要或翻译。纯文字保留换行；支持简单且安全的标题/粗体/列表等 Markdown。含链接、@、HTML、代码等内容一律按字面显示，不执行、不激活；图片、附件、仅面向助手的内容和不识别的结果不转发。SparkX 的周/月报 Skill 本身不是接口成品报告。
- **允许发群的只有账户、币种、分页等都可核验的结构化结果**。官方协议未把单独返回的自由文本绑定到这些字段，因此即使同次响应既有文本又有数据，也绝不把这段文本发到群；本地预览的 `eligible_result=false` 表示原文不可发送，另列 `delivery_preview` 时才是可入队的精确卡片。只有原文而没有已核实数据时，不入队。
- 数据表识别[官方查询返回](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/SKILL.md#response-structure)（包括仅通过 MCP 文本返回的 JSON）里的 `data.rows`，使用[飞书原生纯文本表格](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/table)。保留行序、字段原名、数值、ID、原始比率与币种；`0`、`null`、缺项分开显示。不推算指标，不从领星补数。请求日期、profile、第几页、行数、是否还有下一页与供应端提示单独显示。
- [SparkX 小时数据](https://github.com/SparkXAI/SparkXAI-MCP/blob/b72b914b891ad9ed357dd0a36ab0da53152aae85/skills/sparkx-query-ads-performance/SKILL.md#hourly--ams-data)在 `campaign` 的 `hourly` 及默认小时源的 `keywordPlacement` 中不提供 AI 实绩。卡片在相应字段实际返回时标注 `AISpend` / `AISales` 的数字 `0`、`AIACOS` / `AIROAS` 的 `null` 是占位；普通 `Spend` 和供应端提示照常保留。结构化结果中的整数 `0` 可核验；只有单独返回 JSON 文本时，才用精确十进制解析核验小数零。SDK 可能把结构化的极小非零值解码成小数零，所以即使同页文本也显示为零，仍不能证实结构化原值。出现无法核实的小数零或格式冲突时，表格只在本地预览，不入队；实际 AI 表现须另查日粒度，本命令不会替换或追加查询。
- `asin` 查询的 [Vendor 维度规则](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/references/field-reference.md#vendor-dimension-auto-locking-distributorview_-sellingprogram_)与 Seller 不同。仅在已核实单一账户和币种的成功返回中接受 `meta.appliedDefaults`，且只接受未显式选取/筛选维度的实际默认值：`distributorView=MANUFACTURING`、`sellingProgram=RETAIL`。飞书卡片标注 SparkX 默认、请求筛选或按值分组；不把各口径相加。涉及 `SOURCING` 时，若查询或页面涉及 Ordered 指标，原样显示数值并标明该口径没有实际 Ordered 数据、0 可能是占位；该口径内非零或非数值的 Ordered 返回与文档冲突，不入队。显式筛选仅识别裸字段 `distributorView_` / `sellingProgram_` 的顶层单值等值条件；嵌套、别名、复合或多值条件不自动解释。当两维度均有显式约束、没有默认元数据时，卡片须在每行确认 `storeType_` 为 Vendor，否则只供本地预览；可以在 `select` 加上该字段。发现未知默认键值或返回行与选取、筛选或默认冲突时不发群。请求 Vendor 专用指标即使未返回，也不能在口径缺证时投递。直接 MCP 仍原样中转，不加工返回值。
- `campaignAudience` 支持 `search`（受众名模糊匹配，JX 限 1–200 个可打印字符）且可**省略** `metrics`；只有这个实体适用这两个例外。不补默认指标或改写查询。省略会使上游默认返回全部指标及期间对比列；飞书表格请用裸字段 `select`（如 `["audienceName", "Spend", "prevSpend"]`）明确保留需要的列。供应端忽略无效的请求列不算错误，但实际返回未请求列或大小写不一致时，飞书不入队；超限也不入队、不自动截断。此实体不接收 `groupBy` 或 `queryType`，且最多一个 `orderBy` 规则。该实体的[专用分页](https://github.com/SparkXAI/SparkXAI-MCP/blob/main/skills/sparkx-query-ads-performance/SKILL.md#factentity-campaignaudience--audience-performance)默认每页 50 行，最多 200 行。须将 `meta.total` 与页码、页大小、返回行数及下一页标记核对一致；总行数不等于本页展示行数。
- 每条命令只处理所请求的一页；`hasNextPage=true` 时明确标出，由操作员另选下一页，不拼成全量报表。不返回数据并不证明业务为零。空行集、裸原文或未明确返回实际账户、币种、行数、分页状态等凭据，只能预览。服务端错误、数据矛盾、新的不识别字段/嵌套单元格不生成投递结果。
- 一条消息最多一张表，表格不超过 50 列，每个客户端内页最多显示 10 行；限制双重 JSON 编码后卡片大小为 18 KiB，给 IM 请求封装预留空间。过长/过宽请显式缩小原查询，绝不静默截行、截列或截断金额。

### 经过批准后启用手动投递

从领星采集范围移除店铺，不会撤销独立的 SparkX 手动投递试点；要停发，请删除对应的 SparkX 账户绑定、关闭该店铺的通知，或关闭 SparkX 飞书投递开关。

本步骤仅用于**已单独批准的群和账户**。不要把飞书凭据、队列数据库或路由文件挂进原有只读 MCP 容器。使用受控发布进程和同一受控 outbox 的匹配版本 worker；先以一条小查询核对预览。不要与旧 worker 混跑：旧 worker 不识别 SparkX 专用的投递前复核。不要因为试点而关闭现有生产的静默策略。

小时 AI 护栏只核验新渲染的卡片，不改写已存在的导出。切换版本前应停止试点发布与派发、核对既有待发及未决记录；有尚未核实的旧卡片时保持停发，不自动重新入队或批量重放。

1. 在用于试点的管理员私有路由配置中建立显式绑定。运营台核验测试群成员后，`groups[日报群别名].verified_member_open_ids` 必须有记录、`stores[JX SID].chat_id` 必须显式指向这个日报群别名。以下仅示意新增的**顶级**字段（不要用示例替换现有路由文件）。经管理员核实目标群实际 ID、店铺和币种，填入不可复用的 32 位小写十六进制绑定版本；撤销后重新授权必须换新版本。

```json
{
  "sparkx_profile_routes": {
    "1234567890123456": {
      "store_id": "101",
      "chat_id": "oc_REPLACE_WITH_VERIFIED_GROUP_ID",
      "currency": "USD",
      "generation": "REPLACE_WITH_32_LOWERCASE_HEX_DIGITS"
    }
  }
}
```

2. 发布进程和受控 worker 都须设置 `SPARKX_FEISHU_ENABLED=true`、相同的 `SPARKX_PROFILE_IDS`、应用机器人通知开关/路由。只有生产者需要 SparkX 只读凭据；只有投递 worker 需要飞书应用凭据。投递要求 `FEISHU_ENABLED=true`、`FEISHU_TRANSPORT=app`、非 `APP_DRY_RUN` 和允许手动消息的通知策略。静默、店铺停用、门店币种不一致或目的地失效均不发。此命令不新增店铺记录；店铺状态只作投递权限核对，不作为报告来源。
3. 操作员显式运行 `python -m jx_alert.sparkx_feishu --enqueue < /private/approved-query.json`。同一账户、实际群及查询页的普通请求复用已有 outbox ID。标准输出里的 `PENDING` 只表示已入持久队列；应查询对应 outbox 日志，以 `DELIVERED` **加上实际飞书 message_id** 作为 API 已接受的凭证，不代表群成员已读。请求前、入队前、worker 领取后及飞书应用令牌获取后的提交边界均核实原绑定；超 30 分钟、已撤销、已换群的消息不改投、不补发。本通道只发指定群，不 @ 任何人，不发私聊，不是主动业务异常。若提交后回执丢失，状态为 `DELIVERY_UNKNOWN`，相同绑定和查询不会自动重发或覆盖原文；按[回执核对流程](FEISHU_DELIVERY_RECEIPTS.md)核对原群。
4. 停用时先停止此命令的生产者、撤销私有路由中的绑定，保持新版 worker 运行直至核实没有待处理的 SparkX 消息，再关闭专用开关并重载 worker。未带现行结构化内容版本的旧试点卡片即使仍在队列，也只保留为记录，不会重新标记、改写或发送。不要把尚在等待的消息交给老版本 worker；不要清空数据库、删除数据卷或批量重放历史记录。原 MCP 与原日报保持不变。

### 同一查询页重新导出（不会重试旧通知）

普通 `--enqueue` 对同一查询页和同一实际群返回已有的最新 outbox 编号，
即使已过期、被跳过或送达状态未知。它不会改写旧卡片，也不会因为重新拉取
的数据不同而重发。SparkX 的日粒度（省略、空字符串、大小写不同的 `daily`）
属于同一查询；小时粒度也不区分大小写，但与日粒度分开。实际查询参数不会
被改写。在原账户与群绑定仍符合当前策略时，可以先查询本地记录：

```sh
python -m jx_alert.sparkx_feishu --inspect < /private/approved-query.json
```

当 SparkX 暂不可用时用 `--inspect` 查旧 ID；普通入队命令仍会先核实新的查询结果。
`--inspect` 不访问 SparkX 或飞书、不写队列，仅返回 outbox 编号、状态、版本、
卡片摘要及旧版状态的证据，不返回原卡片。还可以用对应 outbox 编号查询通知日志。

若最新一版已明确终止且能证明**未发出消息 POST**，操作员可以指定其编号，重新
从 SparkX 读取并入队一张新卡片。若前一版已确认送达而仍希望发一份新的快照，
必须同时加 `--ack-delivered`。这不是旧请求的重试：

```sh
python -m jx_alert.sparkx_feishu --enqueue --fresh-after 123 < /private/approved-query.json
python -m jx_alert.sparkx_feishu --enqueue --fresh-after 124 --ack-delivered < /private/approved-query.json
```

- 活动中、回执未知、日志缺失或互相矛盾的记录禁止新建版本。先到原群核对，
  不要把状态改成可重试，也不要轮换绑定版本来绕开。重复同一个 `--fresh-after`
  只返回已经创建的新版，不会再新建。此命令不直接调用飞书。
- 不修改旧 outbox 行、旧 UUID 或旧送达日志。新版使用不同的 outbox 编号和 UUID，
  同时保留其前一版编号。两个发布进程会按账户和实际群串行提交；发生冲突可用
  相同参数重试。所有发布者和派发者必须同时使用兼容版本。
- 旧版记录通过原绑定和原店铺编号重新核对同一个查询摘要，保留原 UUID；轮换绑定
  不会重置导出历史。若原绑定证据缺失，或旧版本已为同一账户、群、查询页生成多条
  并列记录（也包括此前因日粒度写法不同而分开的旧链），系统拦截新建并给出
  阻塞编号。切换版本前须检查此前已排队的试点消息，不要让旧版本与新版并行消费。
  不自动迁移或回填。
