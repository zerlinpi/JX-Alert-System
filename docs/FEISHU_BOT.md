# 飞书问答（DeepSeek + SparkX）

## 谁可以问

- 通知群内 @机器人。
- 私聊机器人：总负责人、广告负责人、通知群管理员、店铺目录里的运营，或当前通知群成员（实时核对群成员）。

问题先写入数据库队列，回调里不做任何模型或 SparkX 请求；独立的工作线程逐条回答，回复挂在原消息下。同一人每分钟最多 3 个问题，排队超过 50 个时群内回复「提问较多」。

## 回答方式

DeepSeek（关闭思考模式，函数调用）在一个 SparkX 会话里最多调用 8 次只读工具：

| 工具 | 对应 SparkX | 限制 |
|---|---|---|
| `list_stores` | 已监控店铺、运营、美西今天与最近完整日 | — |
| `query_ads_performance` | `get_ads_perf` | 只能查已监控的店铺；日数据跨度 ≤ 90 天，小时数据仅 campaign 且 ≤ 7 天；每次 ≤ 100 行；`compare_previous=true` 时服务端查询前一个等长区间并算出变化 |
| `query_entities` | `get_entity_metadata` | campaign / adGroup / target / productAd / portfolio 配置，≤ 100 行 |

服务端为每行补算 ACOS、ROAS、CVR、CPC、CTR 和返回行合计，所以回答需要的数字都能直接引用。最终回答中的每个数字都必须出现在工具结果里：不符合时要求 DeepSeek 重写一次，仍不符合就回复最后一次 SparkX 查询的原始结果，并说明未能核对。

DeepSeek 看不到任何凭据，也没有写操作工具；回答不会声称已修改广告、预算或出价。

## 开启

1. 管理台「DeepSeek」保存 Key，「SparkX 连接」保存 Token。
2. 飞书开放平台为自建应用开启机器人、订阅 `im.message.receive_v1`（长连接）。
3. `.env` 设置 `FEISHU_BOT_ENABLED=true`，启动 `bot` 服务：

```sh
docker compose -p jx-alert -f docker-compose.yml -f docker-compose.runtime.yml --profile bot up -d bot
```
