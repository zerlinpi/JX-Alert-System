# 领星店铺绩效与业绩通知接入

## 为什么需要独立接入

当前 JX Alert 的日常销售、SP/SB/SD 广告快照走领星 OpenAPI。领星公开 OpenAPI 文档中尚未找到可直接用于本项目的店铺绩效列表、绩效详情和业绩通知请求/响应契约，因此不能自行猜测 HTTP 路径或参数。

领星官方 MCP 工具清单列出了以下只读查询工具：

- `customer_store_performance_list`：查询店铺绩效列表
- `customer_store_target_detail`：查询店铺绩效详情
- `customer_performance_notice_list`：查询业绩通知列表

本项目提供了一个只做 `initialize` 和 `tools/list` 的 Schema 探测脚本。它不会调用这些业务工具，也不会修改领星数据。

## 在领星后台获取连接信息

1. 由领星管理员确认帐号已开通 MCP 功能；官方帮助文档说明 MCP 功能需在「设置/业务配置 → 开放接口 → MCP」启用，且受套餐权限限制。
2. 登录领星 ERP，打开 AI 助手中的「管理 MCP」。
3. 复制当前帐号的 MCP Server URL。
4. 复制对应的 `X-Mcp-Key` 鉴权密钥。
5. 确认该帐号本身有权查看需要监控的店铺绩效数据。MCP URL 的数据范围与帐号权限绑定。

官方说明：
- [领星 MCP 使用说明与工具清单](https://www.lingxing.com/help/article/mcp)
- [领星店铺绩效数据说明](https://www.lingxing.com/help/article/performance)
- [领星业绩通知说明](https://www.lingxing.com/help/article/PerformanceNotifications)

## 在服务器配置

只在服务器的 `.env` 文件中填写，不要把密钥提交到 Git，也不要贴到聊天窗口：

```env
LINGXING_MCP_URL=https://openmcp.lingxing.com/mcp-servers/lingxing-mcp
LINGXING_MCP_KEY=替换为领星管理MCP页面复制的密钥
```

然后执行 Schema 探测：

```bash
docker compose run --rm app python scripts/inspect_lingxing_mcp_tools.py
```

脚本只输出目标工具名称、描述和 input schema，不会输出 URL 或密钥。把输出的 Schema 保存下来，下一步才能按真实参数实现自动采集与数据库字段映射。

## 数据更新与依赖边界

- 领星帮助中心说明，店铺绩效页的数据来自亚马逊绩效接口，与 Seller Central 的 Performance / Account Health 数据保持一致，约每 4 小时更新一次。
- 领星帮助中心说明，业绩通知数据由领星插件从店铺后台抓取；插件必须保持登录，系统在指定时段按小时同步。若插件掉线，通知列表可能不再更新。
- 因此，JX Alert 不会把领星店铺状态码当作 Amazon Account Health，也不会把没有新数据误判为「无绩效异常」。
- 正式接入后，绩效数据需保留 source timestamp / sync timestamp，并对过期数据标记 STALE；只有明确完整的响应才能把「没有事件」解释为正常。

## 下一阶段验收标准

1. 三个工具的真实 input schema 已保存并核对。
2. 对每个店铺能读取绩效列表与详情，且保存评分、指标、目标值、状态、更新时间和原始响应。
3. 能读取业绩通知，保存通知 ID、标题/类型、发生时间、处理状态和原始响应。
4. 只有新增的 P0/P1 绩效风险立即通知；同一通知去重，恢复/处理状态可追踪。
5. 插件同步时间过旧或 MCP 查询失败时，明确报数据过期/采集失败，不得报「绩效正常」。
6. MCP 凭证只存在服务器环境变量，不进入日志、Excel、数据库原始快照或通知正文。
