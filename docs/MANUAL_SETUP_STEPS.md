# JX Alert 后续配置与验收步骤

本手册用于维护 `/opt/jx-alert-system`，先核对已有配置，再补充人工确认的信息。北京时间 **18:30 采集、08:00 独立日报提醒、23:30 全维度 Excel 快照**；是否已发布到服务器、下一个实际触发时刻及送达结果，以最终部署回执为准。日报按各店已结束日期读取，不把尚未采集或失败的数据当作零。

保留现有 `.env`、私有凭据、MariaDB、Docker 数据卷和历史记录。下面的编辑步骤只操作非敏感 JSON；不重建环境，不修改广告预算、竞价或状态。现已指定域名 `jxmcp.pacefitday.com`；真实 ChatGPT 授权及工具调用仍须单独验收，见 [小时分析设置](CHAT_ANALYSIS_SETUP.md)。

## 1. 先准备这些信息

| 信息 | 由谁确认 | 未确认时怎么处理 |
| --- | --- | --- |
| 启用的店铺 SID | 店铺负责人 | 目前保留 `5174`；不自动加入其他已授权店铺 |
| 每个新增店铺的真实报表时区、币种 | 店铺负责人结合平台设置核对 | 不先加入采集范围；不要按国家、名称或服务器时间猜测 |
| 需要小时明细采集的 SP 活动 ID | 广告负责人 | 从已同步活动选择，不把所有活动自动列入目标 |
| 测试群管理员 Open ID | 飞书管理员 | `groups.test.admin_open_ids` 保持待填，不填假 ID |
| 店铺运营 Open ID | 运营负责人 | `stores["5174"].open_ids` 保持待填 |
| 管理单聊收件人 Open ID | 管理人员 | `management.open_ids` 保持待填 |
| 商品级负责人 | 运营负责人 | 可选；使用真实 MSKU／ASIN 和店铺 SID |
| 独立 MCP 域名、DNS 和证书 | 域名／服务器维护者 | 保持内网接入，不开放无认证公网接口 |
| 健康接口源时间字段的实际时区 | 领星官方确认 | `lingxing_performance_source_timezone` 保持空字符串 |
| 负曝光记录的含义及处理口径 | 领星官方确认 | 保留原始响应及异常状态，不自行取绝对值、改成零或归因于平台调整 |
| 回测候选的业务真／误报标签 | 熟悉当时业务的审核人 | 未审核保持空白；不自动推定误报率 |

已确认 SID `5174` 的报表时区为 `America/Los_Angeles`。这不代表健康接口源时间也采用该时区。币种保留现有已确认值；若现有值仍未确认，应核实后填写。

## 2. 配置文件与私有文件分开

容器 `/app/data` 对应服务器 `/opt/jx-alert-system/data`。

| 非敏感配置 | 服务器路径 | `runtime-settings.json` 对应字段 |
| --- | --- | --- |
| 店铺启用范围 | `/opt/jx-alert-system/data/sp-runtime/collection-scope.json` | `collection_scope_file` |
| 店铺时区、币种 | `/opt/jx-alert-system/data/sp-runtime/store-timezones.json` | `store_timezones_file` |
| SP 详细采集目标 | `/opt/jx-alert-system/data/sp-runtime/sp-targets.json` | `sp_validation_targets_file` |
| 飞书群与负责人映射 | `/opt/jx-alert-system/data/sp-runtime/feishu-routing.json` | `feishu_routing_file` |
| 运行开关、调度、上述路径、MCP 非敏感设置 | `/opt/jx-alert-system/data/runtime-settings.json` | 此文件自身由应用加载 |

上述路径是当前交接使用的位置，仍应核对运行设置实际指向。容器内的路径应以 `/app/data/...` 开头，不要把宿主机 `/opt/...` 路径填给容器。

`data/secrets/` 中的应用凭据、Token、owner 口令哈希和 OAuth SQLite 不属于这些普通配置。不要 `cat`、复制到聊天、加入 Git，或用配置示例覆盖它们。不要执行会展开所有环境变量的 `docker compose config`、完整 `docker inspect` 或 `Settings.model_dump()` 来排查配置。

## 3. 修改前备份、编辑后校验

### 3.1 只备份这五个非敏感配置文件

在服务器执行，备份目录保留时间戳：

```bash
cd /opt/jx-alert-system
backup_dir="/opt/jx-alert-system/data/config-backups/$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 700 "$backup_dir"
for config_file in \
  data/runtime-settings.json \
  data/sp-runtime/collection-scope.json \
  data/sp-runtime/store-timezones.json \
  data/sp-runtime/sp-targets.json \
  data/sp-runtime/feishu-routing.json
do
  if sudo test -f "$config_file"; then
    sudo cp -a -- "$config_file" "$backup_dir/"
  fi
done
```

不要备份整个 `data/` 到公开位置，不要删除数据库或历史采集文件。修改时用 `sudoedit` 打开具体文件，合并所需字段；本文的小片段不是整文件替换模板。

### 3.2 修改后验证 JSON，不输出文件内容

```bash
sudo docker compose -p jx-alert exec -T app python - <<'PY'
import json
from pathlib import Path

paths = [
    Path('/app/data/runtime-settings.json'),
    Path('/app/data/sp-runtime/collection-scope.json'),
    Path('/app/data/sp-runtime/store-timezones.json'),
    Path('/app/data/sp-runtime/sp-targets.json'),
    Path('/app/data/sp-runtime/feishu-routing.json'),
]
for path in paths:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('root must be an object')
    except (OSError, ValueError) as exc:
        raise SystemExit(f'{path.name}: INVALID ({type(exc).__name__}); stop before restarting')
    print(f'{path.name}: JSON OK')
PY
```

`JSON OK` 仅表示语法和根类型正确，不证明 ID、时区、人员职责或外部权限正确。后续仍要做实际字段校验和业务确认。

## 4. 店铺范围、时区与 SP 目标

### 4.1 先核对新增店铺，再加入范围

现有 `scripts/export_mapping_inventory.py` 可只读查询已授权店铺，导出一张待人工确认的清单；它不启用新店铺。需要时由维护者执行，选一个不存在的新文件名：

```bash
sudo docker compose -p jx-alert exec -T app \
  python /app/scripts/export_mapping_inventory.py \
  --output /app/data/reports/store-mapping-to-review-YYYYMMDD.csv
```

这里的 `YYYYMMDD` 要替换为实际日期。不要用已存在的文件名覆盖上次审核结果。清单中的时区、币种、负责人及“启用”列需要人工补齐；店铺在授权列表中出现，不等于已经获准纳入本系统范围。

### 4.2 店铺时区文件

```bash
sudoedit /opt/jx-alert-system/data/sp-runtime/store-timezones.json
```

结构是 `stores` 对象，每个键为 SID 字符串，例如：

```json
{
  "stores": {
    "5174": {
      "timezone": "America/Los_Angeles",
      "currency_code": ""
    }
  }
}
```

示例空币种表示未知，不是让你清空已有正确币种。确认后填写三字母代码。新增店铺必须分别确认真实 IANA 时区和币种。系统按店铺时区选取已经结束的当地日期，不能将北京时间的“昨天”直接用于所有站点。

### 4.3 店铺启用范围文件

```bash
sudoedit /opt/jx-alert-system/data/sp-runtime/collection-scope.json
```

当前已确认范围的结构：

```json
{"store_ids": [5174]}
```

先准备好新店铺时区、币种及负责人，再把真实 SID 加入 `store_ids`。不要删除该文件、清空配置路径或用缺失文件代替暂停：旧兼容逻辑在范围文件不存在时可能采用全部授权店铺。暂停自动任务应使用调度开关，由维护者实施。

### 4.4 SP 小时明细目标文件

```bash
sudoedit /opt/jx-alert-system/data/sp-runtime/sp-targets.json
```

结构如下，保留已选择的目标：

```json
{
  "targets": [
    {"sid": 5174, "campaign_id": 141469378857743}
  ]
}
```

SID 和活动 ID 必须为真实正整数，组合不重复；SID 必须属于启用范围。活动名称、状态和指标由只读接口同步，不在此手工编造。

`sp-targets.json` 选择详细小时采集与对账目标；它不表示店铺级日报只采这些活动。日报、活动小时、广告组小时、广告小时、投放小时分别保留，不能把四个小时维度相加。请求失败、无返回记录、目标未匹配、明确返回的零值仍要分开。

## 5. 获取并填写飞书人员 ID

### 5.1 使用当前通知自建应用取得 Open ID

人员字段需要 `ou_...` 格式的 Open ID。它与应用绑定，同一用户在不同应用下可能有不同 Open ID；必须使用当前 JX 通知自建应用下的 ID。应用凭据已经单独保存，不需要重新贴到聊天或填入路由文件。

可使用飞书官方 API 调试台的只读流程：

1. 在飞书开放平台进入当前通知自建应用，确认应用版本、目标人员可见范围及所需只读权限。
2. 打开官方“通过手机号或邮箱获取用户 ID”接口页面的 API 调试台，绑定该应用。按接口提示输入待确认人员的邮箱或手机号，以 Open ID 类型取得结果。
3. 将返回人员与实际管理员／运营／管理人员逐一核对，只将已确认的 `ou_...` 写入下方映射。手机号、邮箱、姓名、应用 App ID 都不能直接填入 Open ID 数组。
4. 如果接口权限尚未获批，由管理员按该页要求申请并发布；不要在聊天中粘贴 App Secret 或访问 Token。官方调试台支持绑定应用后获取鉴权凭证，无需把凭据输出到终端。

官方说明列明该查询所用权限为 `contact:user.id:readonly`，并要求应用发版审核；自定义 webhook 本身没有查询人员 ID 的能力。[飞书官方查询方案](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot?lang=zh-CN)、[用户 ID 查询接口](https://open.feishu.cn/document/server-docs/contact-v3/user/batch_get_id)、[API 调试台说明](https://open.feishu.cn/document/client-docs/intro)。

需要核对群会话 ID 时，使用官方“获取用户或机器人所在的群列表”或“搜索对用户或机器人可见的群列表”，核对群名与机器人所在群。[飞书群 ID 说明](https://open.feishu.cn/document/server-docs/group/chat/intro)。**当前“独立站交接群”的目的地已配置，保留 `groups.test.chat_id` 原值，不用为了补人员重新建群或替换群 ID。**

新增 `scripts/map_feishu_operator.py` 仅为本次指定的群内唯一成员做身份核实并批量映射，见 [小时分析设置](CHAT_ANALYSIS_SETUP.md)。`scripts/set_product_owners.py` 只保存已经核对的商品负责人 ID，不负责发现人员或认证授权。

### 5.2 修改负责人映射

```bash
sudoedit /opt/jx-alert-system/data/sp-runtime/feishu-routing.json
```

只补对应字段，保留现有 `default_chat_id`、`groups.test.chat_id` 及其他有效路由：

| 字段 | 填写内容 |
| --- | --- |
| `groups.test.admin_open_ids` | 该群需要被 @ 的管理员 Open ID 数组 |
| `stores["5174"].chat_id` | 群别名 `test`；这里填别名，不填 `oc_...` |
| `stores["5174"].open_ids` | 店铺运营 Open ID 数组 |
| `management.open_ids` | 独立管理单聊收件人的 Open ID 数组 |
| `entities["5174:真实MSKU或ASIN"].open_ids` | 可选的商品级负责人数组 |

数组中可以有多个已确认人员，不能写注释或使用占位 ID。未知时保留空数组，系统会记录映射缺口。

商品级负责人会与店铺负责人、匹配的数据库产品负责人合并去重，**不是覆盖规则**。因此增加 `entities` 不会自动取消店铺层人员的提醒。优先使用含 SID 的联合键，避免不同店铺相同 SKU 相互影响。需要通过现有脚本维护数据库负责人时，优先按店铺＋MSKU 精确选择；按 ASIN 可能匹配多个 MSKU，执行前核对范围，并保持数据库与文件映射一致。

异常群提醒需要管理员和运营映射；管理单聊为独立投递。缺失映射会持久记录为 `ROUTING_FAILED`，不会用假 ID 或不带责任人的消息代替。测试群日常快照可以正常展示数据，但这不能证明管理员 @、运营 @ 和管理单聊都已验收。

### 5.3 只核对新消息，不自动批量补发旧告警

修改映射不会自动发送历史 `ROUTING_FAILED`。若要补发，维护者应先选定具体 outbox ID，核对日期、事件是否仍有效、目的群、实际人员和其他角色的投递状态，再针对该失败角色调用 `repair_application_notification`。修复后产生的可发送任务会由 worker 处理；不要把历史所有记录一次性重放来测试新映射。

## 6. 健康观测模式：保留开启、保持业务告警关闭

当前授权范围是观察归档。运行设置应保留：

```json
{
  "lingxing_performance_enabled": true,
  "lingxing_performance_observation_only": true,
  "lingxing_performance_source_timezone": ""
}
```

这表示可以保存健康／业绩通知原始观测及数据质量状态；不把它解释为健康风险、升级、恢复等业务告警已经启用。即使后续源状态变为 `FRESH`，`observation_only=true` 仍保留该边界。不得为消除 `NOT_CONFIGURED` 而猜填时区，也不要自行关闭观察模式。

请向领星确认不带偏移的 `update_date`、`lastUpdateDate` 的具体时区和夏令时处理。取得明确答复后再填写 IANA 时区，保存确认依据，重新检查新鲜度；是否进一步启用业务告警另行决定。

负曝光记录另行核对：提供经脱敏的请求链路 ID、报告日期／小时、广告或投放 ID、相关数值和原始快照定位。不要发送鉴权头、签名或 Token。当前严格校验不将负指标纳入有效对账；保留证据并等待领星说明，不自行修正原始事实。

## 7. 北京时间 08:00 提醒与 18:30 采集

用户要求的计划是：北京时间 18:30 开始采集，08:00 独立生成并发送日常快照；23:30 保存一套全维度 Excel 快照。异常提醒由独立 outbox worker 处理。采集任务执行成功、日报入队和飞书送达是三项不同证据。

新版调度使用以下非敏感字段，写入 `/opt/jx-alert-system/data/runtime-settings.json`，保留文件中其他字段：

```bash
sudoedit /opt/jx-alert-system/data/runtime-settings.json
```

| 字段 | 应用值 | 含义 |
| --- | --- | --- |
| `daily_sync_cron` | `"30 18 * * *"` | 每天北京时间 18:30 持久排队采集；该任务固定使用 `Asia/Shanghai` |
| `feishu_daily_report_cron` | `"0 8 * * *"` | 每天北京时间 08:00 独立日报；该任务固定使用 `Asia/Shanghai` |
| `feishu_daily_report_start_date` | 维护者启用时填写的 `YYYY-MM-DD` | 下一次 08:00 窗口的北京时间日期；限制首次启用及补发范围 |

首次开始日期由维护者按实际启用窗口写入，不照抄示例日期。以上新字段需要对应新版服务；不要在旧 `0feb56b` 版本只改一个 `daily_sync_cron` 就宣称实现了独立 08:00 日报。最终是否已发布、下一触发时刻及送达结果仍以部署回执为准。

08:00 日报只读取已经保存的数据库，不等待采集完成；当日缺数据或采集未完成时也会发送缺失提示，保留实际覆盖状态，不伪造“完整”。数据归属按每个店铺已经结束的当地日期计算。SID `5174` 的 `America/Los_Angeles` 在冬令时和夏令时下，北京时间 08:00 选取的完整当地报告日均为**北京时间日期的前两天**，不是北京时间“昨天”。例如北京时间 11 月 14 日 08:00 的完整日报日期为店铺当地 11 月 12 日。

如果服务错过当天 08:00，且启动日不早于 `feishu_daily_report_start_date`，会在启动时及每分钟的补偿检查中按幂等键补发一次，并标注实际生成时刻；超过一分钟生成的消息标注“延迟补发”。跨日尚未送达的旧定时日报保留为过期记录，不在下一天早晨补发历史日报；当天的新日报单独生成。定时触发不代表网络秒级送达；仍需记录日报入队和飞书实际送达时间。该调度使用北京时间和已确认的店铺报表时区，**不需要填写健康接口源时区**；`lingxing_performance_source_timezone` 在领星确认前继续留空。

## 8. 检查有效配置并重启对应进程

完成前述 JSON 语法校验后，用应用正常配置加载方式检查**白名单字段**。不要打印全部 Settings、环境变量或私有文件内容：

```bash
sudo docker compose -p jx-alert exec -T app python - <<'PY'
import json
from jx_alert.config import get_settings
from jx_alert.storage.store_config import load_store_config
from jx_alert.notifications.routing import _load_routing

settings = get_settings()
fields = [
    'app_dry_run', 'scheduler_enabled', 'app_timezone', 'daily_sync_cron',
    'feishu_daily_report_cron', 'feishu_daily_report_start_date',
    'collection_scope_file', 'store_timezones_file', 'sp_validation_targets_file',
    'feishu_routing_file', 'feishu_enabled', 'feishu_transport',
    'feishu_daily_report_enabled', 'feishu_daily_report_chat_id',
    'lingxing_performance_enabled', 'lingxing_performance_observation_only',
    'lingxing_performance_source_timezone', 'mcp_auth_mode', 'mcp_public_url',
]
print(json.dumps({name: getattr(settings, name) for name in fields}, ensure_ascii=False))
load_store_config(settings.store_timezones_file)
_load_routing(settings.feishu_routing_file)
print('Timezone and routing structure: OK; recipient identity still requires human verification')
PY
```

这一步不发消息、不触发采集。JSON 合法不代表所有映射齐全；只有实际管理员和运营 @、管理单聊各成功一次，才可记录相应验收通过。

| 变更 | 对应进程 |
| --- | --- |
| 只修改范围、时区、目标或路由文件内容，路径未变 | 下一次读取时使用新内容；选择维护窗口重启 `scheduler`，并在下次采集确认数据库时区／币种同步 |
| 修改运行开关、调度或配置路径 | 重启 `app` 和 `scheduler`，清除进程内配置缓存 |
| 修改 MCP 模式、域名、允许主机／来源或反代来源 | 准备完整后重启 `mcp`；应用若也展示配置状态，可同时重启 `app` |

按实际变更选择命令，不重启数据库：

```bash
sudo docker compose -p jx-alert restart app scheduler
sudo docker compose -p jx-alert ps
```

仅 MCP 改动时使用：

```bash
sudo docker compose -p jx-alert --profile mcp restart mcp
```

`restart` 不会重建镜像或数据卷；若维护者尚未启动某个 profile 服务，应先按现有部署说明确认，而不是用重建环境解决。不要执行 `down -v`。重启发送进程前先核对待发送队列；启用的 worker 会处理可发送任务。

## 9. 独立域名与 ChatGPT MCP

完整接入说明见 [CHATGPT_MCP.md](CHATGPT_MCP.md)。域名尚未提供时保留当前受认证的内网服务，不选择无认证，不把 Token 拼到 URL。

### 9.1 域名、DNS、TLS

1. 提供专用域名，核对服务器实际公网地址后设置 DNS；只有确实可达的 IPv6 才发布 AAAA 记录。
2. 取得有效 TLS 证书，按 `deploy/nginx-mcp.conf.example` 新建独立虚拟主机；保留其他站点。后端 MCP 仍绑定本机端口。
3. 代理应覆盖整个 OAuth／MCP 服务，包括资源元数据、授权元数据、`/authorize`、`/register`、`/token`、`/revoke`、`/oauth/owner` 和 `/mcp`。不能只代理 `/mcp`。
4. 将 `mcp_public_url` 设置为真实 HTTPS origin；对应 `mcp_allowed_hosts`、`mcp_allowed_origins` 与该域名一致。按实际 Docker 网关设置 `mcp_forwarded_allow_ips`，不要填 `*` 或照抄其他服务器地址。只查询容器网络网关字段，避免输出完整容器配置。
5. `sudo nginx -t` 通过后才加载该专用配置。配置和证书均应由维护者在本机处理，不输出私钥内容。

### 9.2 私有交互初始化 owner 口令

确认 owner 文件尚未创建后，在服务器交互执行现有脚本：

```bash
cd /opt/jx-alert-system
sudo docker compose -p jx-alert exec app python /app/scripts/setup_mcp_owner.py
```

脚本通过 `getpass` 输入并确认至少 16 字符口令，不回显；首次仅写随机盐与 PBKDF2-SHA256 哈希，不覆盖已有文件。不把口令放在命令参数、聊天、`.env` 或 Git 中。

默认私有文件对应服务器 `data/secrets/mcp-owner.json` 和 `data/secrets/mcp-oauth.sqlite3`，在 Linux 上应为服务用户所有、权限 `0600`。不要输出文件或数据库内容；OAuth 状态库还保存协议所需的私有客户端凭据。若 owner 文件已存在，应由维护者处理恢复流程，不能删除文件、重设身份来跳过登录。

### 9.3 先读权限，再考虑采集权限

域名、TLS、私有文件准备并检查后，维护者再将运行模式切换为 OAuth、重启 MCP，验证匿名请求为 401、元数据正确及所有者授权完整流程。

官方当前网页入口：**Plugins → ＋ → Add custom MCP server**，连接选择 **Server URL**，填真实 HTTPS `/mcp` 地址，认证选择 **OAuth** 并使用本服务支持的 DCR。创建后安装私有插件，在对话中通过 `@` 选择。账户或工作区权限可能限制入口；以实际界面可见权限为准。[OpenAI 官方连接说明](https://developers.openai.com/api/docs/guides/custom-mcp-server)

第一次只授予 `jx:read`，依次检查系统健康、配置缺口、店铺、覆盖和已存指标。确认确实需要请求回补后，再授权 `jx:collect`，并核对服务端操作开关和调度器。它只请求已配置范围的采集任务，不提供广告修改能力。[OpenAI 官方 OAuth 要求](https://developers.openai.com/plugins/build/auth)

可以先这样提问：

> @JX Alert 只查询系统状态、手工配置缺口和店铺5174最近完整日期的数据覆盖；缺失值保留未知，不触发采集。

取得采集权限后，使用明确日期范围和新的幂等键请求一次回补，记录返回任务号，再查看任务状态。不要因请求已入队就写为采集已成功。

只有在真实 ChatGPT 中完成授权、列出工具并成功查询一次，才记录外部 MCP 验收通过。域名、DNS、TLS、单元测试和本机初始化分别通过，都不能代替这一项。

## 10. 人工审核 294 条历史候选

使用交付的《规则回测人工审核表.csv》，核对它是保留唯一审核键及广告 ID 的新表。旧导出曾存在多个广告共用展示 SKU 导致审核键歧义的问题；不能把旧标签按 SKU 批量迁移，也不能因为展示名相同就删除记录。

逐条核对报告日、店铺／活动／广告 ID、MSKU／ASIN、规则、七日基线、当前值、原始响应和当时业务背景。在 `review_label` 中仅填写：

- `true_positive`：审核人结合原始数据和业务背景确认应作为有效业务告警。
- `false_positive`：审核人确认不应作为业务告警，并保留可复核的原因与证据。
- 空白：尚未审核、资料不足或结论待确认。

294 是候选数量，不是已确认事故数或误报数。数值符合 ±15% 等触发条件，也不自动证明业务告警有效；计划促销、低样本和缺失实体都需要结合证据判断。当前修订后的历史回放不等于“当日当时所知数据”的重现。

保存原审核键、审核人、时间与依据；不更改原始指标来配合结论。将审核后的表交维护者按 `key` 形成 `labels` 参数进行隔离复核；现有 `backtest_history` 支持该参数，未提供一条可直接导入任意 CSV 的现成命令。未审核行不进入真／误报分母，全部未审核时误报率应为未知，不是 0。

## 11. 每项完成后保留什么证据

记录变更时间、操作者、所用代码版本、配置备份目录和本次修改项；不记录密码、Token、私钥或完整凭据。

| 验收事项 | 需要的实际证据 |
| --- | --- |
| 新店铺启用 | 已确认时区／币种／负责人，范围文件正确，下一次任务覆盖该店铺并保留采集状态 |
| 管理员及运营提醒 | 新消息到正确群，两类人员实际收到 @；outbox 和投递日志可对应 |
| 管理单聊 | 正确管理人员收到独立消息；不以群消息代替 |
| 18:30／08:00／23:30 调度 | 最终发布配置、下一触发时刻、采集开始／结束、日报实际送达和 Excel 完整清单 |
| 健康观测 | 原始观测归档、观察模式保持 true；源时区未确认仍如实显示未知 |
| 领星待确认项 | 关于源时区和负曝光的官方答复及其对应请求／快照 |
| MCP | 真实 HTTPS、匿名 401、OAuth 成功、ChatGPT 中查询成功；采集权限单独验证 |
| 历史审核 | 人工标签及依据；未审核与已审核数量分别统计 |

以上是后续操作说明，不是这些步骤已经执行或全部验收通过的声明。
