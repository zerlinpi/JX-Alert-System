# Linux 云服务器部署

## 1. 服务器准备

建议使用受支持的 Linux LTS 发行版，安装 Docker Engine 与 Docker Compose Plugin。开放 SSH 端口；API 默认只监听 `127.0.0.1`，不要直接把 8000 端口暴露给公网。

## 2. 拉取项目与配置

```bash
git clone https://github.com/zerlinpi/JX-Alert-System.git
cd JX-Alert-System
cp .env.example .env
chmod 600 .env
```

使用 `openssl rand -hex 32` 分别生成 `DB_PASSWORD` 和 `DB_ROOT_PASSWORD`，不要复用密码。编辑 `.env`，先保持：
- `APP_DRY_RUN=true`
- `FEISHU_ENABLED=false`
- `SCHEDULER_ENABLED=false`

如果以前使用旧模板，请删除旧的 `DATABASE_URL` 行，确保应用使用 DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD 组装数据库连接。

## 3. 挂载目录权限

容器以 UID/GID 10001 的非 root 用户运行：
```bash
mkdir -p data
cp config/feishu-routing.example.json data/feishu-routing.json
cp config/store-timezones.example.json data/store-timezones.json
sudo chown -R 10001:10001 data
chmod 700 data
chmod 600 data/feishu-routing.json data/store-timezones.json
nano data/store-timezones.json
```

## 4. 首次启动

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f app
curl http://127.0.0.1:8000/health
```

如果需要外部访问，推荐在本机 Nginx/Caddy 配置 TLS 反向代理到 `127.0.0.1:8000`，并添加访问认证或 IP 限制。不要把未鉴权的管理 API 暴露公网。

## 5. 开启调度器

每日只读采集、异常分析和 Excel 导出已经接入。首次上线前必须先完成领星 Token 接口配置、时区映射和至少 22 天历史回补；22 天覆盖 SB/SD 的 14 天归因窗及其前七日基准。若账号仅使用 SP，可将 `AD_ATTRIBUTION_REFRESH_DAYS` 与回补天数调整为至少 15 天。检查数据完整性及报表后，再启用自动调度。

完成历史回补、检查异常规则和 Excel 结果，并确认飞书 Webhook 测试成功后，才设置：
```env
SCHEDULER_ENABLED=true
FEISHU_ENABLED=true
APP_DRY_RUN=false
```

随后启动可选 scheduler profile：
```bash
docker compose --profile scheduler up -d --build scheduler
docker compose logs -f scheduler
```

保持 scheduler 只运行一个实例。不要横向扩容 scheduler 服务，否则可能产生重复处理。启用后，调度器会按 DAILY_SYNC_CRON 执行只读采集、异常分析、每店 Excel 导出，并每 10 秒处理飞书 outbox。

## 6. 常用维护命令

```bash
docker compose ps
docker compose logs --tail=200 app
docker compose logs --tail=200 scheduler
docker compose restart app
docker compose down
docker compose up -d --build
```

## 6. 启动远程 MCP 查询服务（可选）

MCP 服务只开放只读查询工具，不会触发领星采集，也不能修改广告。先生成随机 Bearer Token：
```bash
openssl rand -hex 32
nano .env
```

将生成值写入 `MCP_BEARER_TOKEN`。默认：
- MCP 容器监听 `0.0.0.0:8001`；
- Docker 只将端口发布到宿主机 `127.0.0.1:8001`；
- MCP 路径为 `/mcp`；
- 未设置域名白名单时，仅允许 localhost Host/Origin。

启动服务：
```bash
docker compose --profile mcp up -d --build mcp
docker compose logs -f mcp
```

本机 MCP URL：`http://127.0.0.1:8001/mcp`。客户端请求必须带：
```http
Authorization: Bearer <MCP_BEARER_TOKEN>
```

如果需要通过公网域名提供给远程 MCP 客户端：
1. 配置 Nginx/Caddy TLS 反向代理到 `127.0.0.1:8001`，关闭响应缓冲并允许长连接。
2. 将实际域名及带端口形式加入 `MCP_ALLOWED_HOSTS`，例如 `mcp.example.com,mcp.example.com:*`。
3. 如果使用浏览器型客户端，再把其准确来源加入 `MCP_ALLOWED_ORIGINS`。
4. 确认反向代理透传 `Authorization` 请求头。
5. 保持 8001 端口只对本机开放，不要直接将容器端口映射到公网。

`MCP_BEARER_TOKEN` 至少 32 字符，建议使用 `openssl rand -hex 32` 生成。不要把它放入 URL、Git、日志或聊天。若 Token 泄露，应立即替换并重启 MCP 服务。

## 7. 数据备份

数据库备份示例：
```bash
mkdir -p backups
docker compose exec -T db sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" jxalert' > "backups/jxalert-$(date +%F-%H%M).sql"
```

同时备份服务器上的 `data/` 目录（其中包含原始数据、报表和权限为 0600 的领星 Token 缓存）。备份文件应加密并限制访问权限。恢复前先停止 app 和 scheduler，再导入 SQL。

## 8. 安全注意事项

- `.env`、领星 AppSecret、Token、飞书 Webhook URL、飞书签名密钥不得提交 Git。
- 领星开放平台需要把服务器公网出口 IP 加入 IP 白名单；不要填容器内网 IP。
- 定期更新系统、Docker 镜像和 Python 依赖。
- 数据库端口不映射到公网。
- 配置服务器时间同步（NTP），避免领星签名或飞书 webhook 签名时间偏差。
- 设置日志轮转、磁盘空间监控、数据库备份和恢复演练。
