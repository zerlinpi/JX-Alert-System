# 部署、迁移与回滚

## 服务

| Compose 服务 | 命令 | 说明 |
|---|---|---|
| `db` | MariaDB 11 | 数据卷 `jx-alert_db_data`，不删除 |
| `app` | `uvicorn jx_alert.api:app` | `/health`：数据库、SparkX/DeepSeek 是否配置、是否发送 |
| `scheduler` | `python -m jx_alert.scheduler` | 08:30 日报与复盘、每小时监控、10 秒一次的发送队列 |
| `console` | `python -m jx_alert.admin_console` | 管理台，监听 `127.0.0.1:8002`；服务器上同时占用原 MCP 端口（默认 8001） |
| `bot` | `python -m jx_alert.feishu_bot` | 飞书长连接问答 |

旧版本的 `mcp` 服务（ChatGPT 连接器）已移除。需要在 ChatGPT/Claude 中查广告数据时，可按 SparkX 官方说明直接连接 SparkX MCP。

## 服务器不需要改的配置

- **Nginx 不用改。** 现有站点把 `/console`、`/oauth/owner` 转到 8002，其余路径转到原 MCP 端口。新版本的 `docker-compose.runtime.yml` 让管理台同时监听这个端口：`/` 跳转到 `/console/`，`/mcp`、`/authorize`、`/token`、`/.well-known/oauth-*` 等旧路径返回 410 和停用说明，不再出现 502。想精简时可换成 `deploy/jxmcp.pacefitday.com.conf`（只转发 `/console`），属于可选项。
- **`.env` 不用改。** `MCP_PUBLIC_URL`、`MCP_OWNER_CREDENTIALS_FILE`、`MCP_FORWARDED_ALLOW_IPS` 继续作为管理台设置使用，`MCP_BIND_IP`/`MCP_BIND_PORT` 决定兼容端口。
- **`runtime-settings.json` 不用改。** 本版本不使用的字段（包括旧版本留下的）一律忽略，每个进程启动后在日志里列出一次字段名；`mcp_public_url`、`mcp_owner_credentials_file`、`mcp_forwarded_allow_ips` 仍作为管理台设置生效。文件不会被改写。
- **不需要重新构建镜像。** `app`、`scheduler`、`console` 用现有 `jx-alert-app` 镜像挂载 `./src`，`bot` 用现有 `jx-alert-bot` 镜像。
- 原所有者口令 `data/secrets/mcp-owner.json`、管理台会话文件、`feishu-routing.json`（通知群、总负责人、广告负责人）原样沿用。`feishu-routing.json` 里旧版本的店铺映射（`stores` 段）保留但不再读取：升级后在管理台「店铺与运营」为店铺指定运营，指定之前照常通知、不 @ 人。
- 数据库只新增、不删除：旧版本建立的其他表原样保留，本版本不读取、不写入；`notification_outbox`、`notification_logs`、`sync_jobs` 继续使用。发送队列里旧版本的待发消息不会发送，标记为「已跳过」。

## 升级（`deploy/upgrade.sh`）

脚本依次执行：只读检查 → 备份数据库与 `data/*.json` → 停止 scheduler、bot 和旧 mcp 容器（容器保留，不删除）→ 切换到指定提交并核对 tree → 启动 app、console → 检查 `/health`、8002 与兼容端口上的登录页、旧 `/mcp` 返回 410 → 启动 scheduler、bot → 核对消息与任务表行数没有减少。任何一步失败都会停下并打印回滚命令。

以下检查会让脚本拒绝执行（`--force` 可跳过后两项）：

- 目标提交不在服务器仓库，或其 tree 与测试通过的 tree 不一致。
- `jx-alert-app` 或 `jx-alert-bot` 镜像不存在、目标 Compose 文件校验失败、数据库容器未运行。
- 仓库里有被修改的受跟踪文件。
- 旧版本仍有 6 小时内开始、状态为 RUNNING 的任务（例如 18:30 的采集）。

### 1. 把提交带到服务器

代码在 GitHub 上时，在服务器执行 `git fetch origin`。仅在本地的提交用 git bundle 传送（服务器已有 `29055f7`，bundle 只含之后的提交）：

```powershell
# 工作站（PowerShell）
git bundle create $env:TEMP\jx-sparkx.bundle 29055f7..local/sparkx-only
git rev-parse local/sparkx-only; git rev-parse "local/sparkx-only^{tree}"   # 记下提交与 tree
scp $env:TEMP\jx-sparkx.bundle ubuntu@43.153.88.94:/tmp/
```

```sh
# 服务器
cd /opt/jx-alert-system
sudo git -c safe.directory=$PWD bundle verify /tmp/jx-sparkx.bundle
sudo git -c safe.directory=$PWD fetch /tmp/jx-sparkx.bundle local/sparkx-only:refs/jx/sparkx-only
```

### 2. 预检，再执行

```sh
SHA=<提交>; TREE=<tree>
sudo git -c safe.directory=/opt/jx-alert-system -C /opt/jx-alert-system show $SHA:deploy/upgrade.sh | sudo tee /tmp/jx-upgrade.sh >/dev/null
sudo bash /tmp/jx-upgrade.sh --dry-run $SHA $TREE      # 只读：检查并打印计划
sudo nohup bash /tmp/jx-upgrade.sh $SHA $TREE > /tmp/jx-upgrade.log 2>&1 < /dev/null &
tail -f /tmp/jx-upgrade.log                            # SSH 断开不影响脚本
```

### 3. 升级后

三条通知默认开启，但 SparkX Token 保存之前不会运行，所以升级本身不会发送消息。保存 Token 后：实时监控最迟约 10 分钟内开始发到通知群（北京时间 09:00–24:00），日报和复盘在 08:30–10:00 之间发送；没有指定运营的店铺照常通知，不 @ 人。想先核对内容：先在「通知设置」关闭通知，再保存 Token，预览确认后重新打开（预览需要 Token）。按 [管理台首次配置顺序](CONSOLE.md#首次配置顺序) 操作。问答机器人在 SparkX Token 保存前会回复「尚未配置 SparkX MCP Token」。

## 回滚（`deploy/rollback.sh`）

```sh
sudo cp /opt/jx-alert-system/deploy/rollback.sh /tmp/jx-rollback.sh
sudo bash /tmp/jx-rollback.sh <升级前的提交>   # 升级日志和 backups/pre-sparkx-*/previous-commit 中都有
```

脚本先停止新的 scheduler、bot、console（释放兼容端口），切回旧提交，用现有镜像启动 app、mcp、console（旧版 console 用 `jx-alert-console:latest`），健康后再启动 scheduler、bot。数据库、数据卷、`data/` 以及新版本建立的 `data/sparkx-stores.json`、`data/jx-notifications.json` 都保留，不影响旧版本。如果 Nginx 曾换成精简配置，恢复修改前的配置并 `nginx -t` 后重新加载。

需要恢复数据库时使用升级前的备份 `backups/pre-sparkx-<时间>/jxalert.sql.gz`；回滚代码本身不需要恢复数据库。
