# 店铺运营台

在已配置的 MCP HTTPS 域名打开 `/console/`，用现有的所有者口令登录。管理会话有效期 30 分钟，退出后立即失效。运营台和 Chat MCP 使用各自的会话。

在「经营概览」按名称或店铺 ID 找到店铺。目标日尚未采集时，默认可展示近七日最近一份完整日报，页面分别列出实际展示日和最近已结束日。目标日采集失败时保留失败提示。可以选择其他已结束日期。报表包含订单、销量、销售额、SP/SB/SD 活动和采集覆盖、七项广告指标、近 14 日趋势与绩效观测。广告指标各自标出报表或成熟归因日期。

在「人员映射」选定店铺后，点击加载飞书成员。勾选对应人员并点击「保存映射」。保存时核对该群的成员身份和当前配置版本，更新店铺负责人并留存修改前的私有备份。店铺原有群路由继续使用。启用群回退的店铺可清空负责人；清空后按现有群通知策略处理。原有映射在保存前继续生效。

在店铺详情的「店铺开关」中分别设置「数据采集」和「飞书通知」，然后点「保存开关」。两项默认开启。关闭采集会跳过该店铺后续采集与回补，已开始的请求可能完成。关闭通知会从新日报和绩效快报中排除该店铺，并跳过该店铺的告警和 Chat 推送。已排队的合并卡片如果包含该店铺，会整张跳过。恢复后从后续任务继续，历史待发消息保留记录。店铺历史、Excel 归档与用户主动在飞书群内发起的查询仍可用。

Chat 直接发送 Markdown 报告时，`store_ids` 填写正文覆盖的全部店铺编号。有店铺关闭通知时，需在正文和编号列表中移除该店铺。

开关保存在服务共享目录的 `store-controls.json`，与店铺采集范围及人员映射分开。保存时生成修改前的备份，重启后沿用已保存设置。

## 部署

线上服务器沿用现有 OAuth 配置、Compose 项目名与运行配置，在项目目录启动浏览器服务：

```sh
cd /opt/jx-alert-system
sudo docker compose -p jx-alert -f docker-compose.yml -f docker-compose.runtime.yml --profile console up -d --build --no-deps console
```

`docker-compose.runtime.yml` 是服务器已有运行配置，必须保留。省略项目名或运行配置可能创建其他 Compose 项目，或使服务无法连接现有数据库。其他安装环境应沿用其原有项目名和配置文件。容器只映射本机 `127.0.0.1:8002`，与现有 MCP 使用同一个受保护的数据目录。管理登录沿用所有者口令校验；管理会话保存到单独的私有 SQLite 文件。

在现有 HTTPS 站点内添加 `location = /console`、`location ^~ /console/` 与 `location = /oauth/owner`，将这三项代理至 `http://127.0.0.1:8002`。沿用现有 TLS、代理信任和安全日志方案。设置 `Host` 为 `$host`、`X-Forwarded-Proto` 为 `$scheme`，并沿用站点对真实客户端 IP 的可信传递。`/authorize`、`/token`、`/mcp` 等继续由 MCP 服务处理。OAuth 页面读取现有短期授权请求，回跳后由原 MCP 服务完成换令牌。

上线前运行 `nginx -t`，并验证管理台未登录时重定向到登录页、未登录 API 返回 401、HTTPS 授权页和 MCP 元数据可访问。关闭时恢复修改前的 Nginx 配置，通过 `nginx -t` 后重新加载，再停止浏览器服务：

```sh
sudo docker compose -p jx-alert -f docker-compose.yml -f docker-compose.runtime.yml --profile console stop console
```

原路由配置备份保留在 `feishu-routing.json` 所在目录。停用运营台不删除数据库、人员映射备份或 Docker 数据卷，原 MCP、采集和通知服务继续运行。
