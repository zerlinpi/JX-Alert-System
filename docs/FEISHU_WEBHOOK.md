# 飞书群自定义机器人 Webhook

本项目使用群自定义机器人，不使用企业自建应用机器人。自定义机器人是单向 Webhook，且一个 Webhook 只能向创建它的群推送消息。

## 1. 在飞书创建机器人

在目标群中打开「设置 → 群机器人 → 添加机器人 → 自定义机器人」，创建后复制 Webhook URL。Webhook URL 相当于发送凭据，泄露后他人可能向该群发消息。

## 2. 建议开启签名校验

进入机器人设置中的安全设置，启用「签名校验」并复制密钥。将其填写到 Linux 服务器本地的 `.env`：

```env
FEISHU_ENABLED=false
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的真实token
FEISHU_SIGN_SECRET=你的签名密钥
```

如果机器人启用了自定义关键词，还要把关键词填写到 `FEISHU_KEYWORD`。如果没有启用关键词校验，留空即可。

签名计算按飞书文档：以 `timestamp + "\\n" + secret` 作为 HMAC-SHA256 的 key，对空字符串签名，再 Base64 编码。请求体携带 `timestamp` 和 `sign`。服务器需保持时间同步。

## 3. 指定人员 @ 与路由

自定义机器人文本消息支持：
```text
<at user_id="ou_xxxxxxxxx">成员姓名</at>
```

把成员真实 Open ID 填入路由文件。先复制模板：
```bash
cp config/feishu-routing.example.json data/feishu-routing.json
chmod 600 data/feishu-routing.json
nano data/feishu-routing.json
```

配置结构：
- `stores`：领星店铺 ID → 店铺负责人 Open ID 列表。
- `entities`：`店铺ID:MSKU/ASIN` 或全局 MSKU/ASIN → 产品负责人 Open ID 列表。
- `FEISHU_DEFAULT_AT_OPEN_IDS`：所有告警都 @ 的管理员 Open ID，多个用英文逗号分隔。

注意：
- 必须是机器人所在群的成员 ID。
- 外部群聊仅支持 Open ID。
- 手机号、邮箱、昵称不能直接代替 Open ID。
- 自定义机器人没有查询用户资料或群成员的权限，因此需要从飞书管理界面/已有合法数据中取得 Open ID。

## 4. 本地配置检查与真实测试

默认命令只检查本地配置，不发送消息：
```bash
python scripts/test_feishu_webhook.py
```

确认 Webhook URL、签名密钥、关键词与群成员 Open ID 无误后，才手动发送一条真实测试消息：
```bash
python scripts/test_feishu_webhook.py --send
```

真实测试会向该 Webhook 对应的群发送消息。不要在自动部署或 CI 中使用 `--send`。

## 5. 限流与重试

飞书自定义机器人文档标注频率限制为单租户单机器人 100 次/分钟、5 次/秒，单条请求体不超过 20 KB。本项目 worker 小批量串行发送并做指数退避。网络超时可能出现“飞书已收到但客户端未收到响应”的不确定状态，后续重试存在重复消息可能。
