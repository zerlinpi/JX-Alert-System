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
sudo chown -R 10001:10001 data
chmod 700 data
cp config/feishu-routing.example.json data/feishu-routing.json
chmod 600 data/feishu-routing.json
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

当前日采集任务仍是 fail-closed 占位，真实采集适配、历史回补和接口契约验证完成之前，不要将 `APP_DRY_RUN` 设为 false。

只有确认飞书 Webhook 和 outbox worker 可用后，才设置：
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

保持 scheduler 只运行一个实例。不要横向扩容 scheduler 服务，否则可能产生重复处理。当前采集流水线尚未接入，开启 scheduler 不会自动拉取领星数据；它只会按配置运行占位日志和通知 outbox worker。

## 6. 常用维护命令

```bash
docker compose ps
docker compose logs --tail=200 app
docker compose logs --tail=200 scheduler
docker compose restart app
docker compose down
docker compose up -d --build
```

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
