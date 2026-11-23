# 部署、迁移与回滚

## 服务

| Compose 服务 | 命令 | 说明 |
|---|---|---|
| `db` | MariaDB 11 | 数据卷 `jx-alert_db_data`，不删除 |
| `app` | `uvicorn jx_alert.api:app` | `/health`：数据库、SparkX/DeepSeek 是否配置、是否发送 |
| `scheduler` | `python -m jx_alert.scheduler` | 08:30 日报与复盘、每小时监控、10 秒一次的发送队列 |
| `console` | `python -m jx_alert.admin_console` | 管理台，仅映射 `127.0.0.1:8002`，由 Nginx 反代 `/console` |
| `bot` | `python -m jx_alert.feishu_bot` | 飞书长连接问答 |

原 `mcp` 服务（领星数据的 ChatGPT 连接器）已移除。需要在 ChatGPT/Claude 中查广告数据时，可按 SparkX 官方说明直接连接 SparkX MCP。

## 从领星版本升级

1. 备份：`mysqldump` 全库、`data/` 目录（含 `secrets/`、`feishu-routing.json`、`runtime-settings.json`）。
2. 拉取已测试的版本，`docker-compose.runtime.yml` 中 `scheduler`、`console` 挂载 `./src`，更新代码后重启即可：

```sh
cd /opt/jx-alert-system
sudo docker compose -p jx-alert -f docker-compose.yml -f docker-compose.runtime.yml \
  --profile scheduler --profile console --profile bot up -d --no-build app scheduler console bot
sudo docker stop jx-alert-mcp-1   # 新 Compose 文件已无 mcp 服务；停用旧容器，不删除其数据
```

3. Nginx：按 `deploy/jxmcp.pacefitday.com.conf` 只保留 `/console` 反代到 8002，`nginx -t` 通过后重新加载。
4. 管理台按 [首次配置顺序](CONSOLE.md#首次配置顺序) 配置并预览，确认后再开启通知。

兼容性：

- 现有 `runtime-settings.json` 中领星、MCP、Excel 等旧字段会被忽略（不改写文件）；`mcp_public_url`、`mcp_owner_credentials_file`、`mcp_forwarded_allow_ips` 自动作为管理台设置使用。环境变量 `MCP_PUBLIC_URL` 等同 `CONSOLE_PUBLIC_URL`。
- 原所有者口令文件 `data/secrets/mcp-owner.json` 继续用于管理台登录。
- 数据库只新增、不删除：领星时期的表（`stores`、`daily_metrics`、`campaigns` 等）原样保留，不再写入；`notification_outbox`、`notification_logs`、`sync_jobs` 继续使用。
- 发送队列里领星时期的待发消息不会发送，标记为「已跳过」保留。
- `feishu-routing.json` 的群、总负责人、广告负责人原样沿用；旧的按领星店铺 ID 的负责人映射保留，只用于「采用全部建议运营」。

## 回滚

1. `git checkout` 回到升级前的版本（代码回滚不删除新文件和新记录）。
2. `sudo docker compose -p jx-alert ... up -d mcp scheduler console bot` 恢复旧服务；恢复旧 Nginx 配置后 `nginx -t` 并重新加载。
3. 新增的 `data/sparkx-stores.json`、`data/jx-notifications.json` 与 `sync_jobs` 中的新记录保留，不影响旧版本。
4. 从不删除 Docker 数据卷。
