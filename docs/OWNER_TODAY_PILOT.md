# 单店、单截点：核对时区与采集依据（仅离线）

用于回答一个有限的问题：**这份封存副本里的两次采集，能否按当时确认的同一店铺时区做暂估回放？** 如果没有当时独立留存的依据，答案是“无法验收”。普通预览和 `--as-of` 回放无需这些文件，原输出不变；两者都不核验时区来源。不能把它们当成降级通过。

```sh
PYTHONPATH=src python scripts/audit_owner_today.py --sqlite-copy sealed.sqlite \
  --scope-file one-shop.json --as-of 2024-11-22T06:30:00Z \
  --pilot-capture capture.json --timezone-evidence authority.json
```

**输入均为私下保管的本地文件。** 不抓取、不生成、不回填或上传证据。提供范围获准、静止、独立的 SQLite 备份，不得在运行中的库或可被他人替换的目录上使用。以下为虚构结构；哈希占位符不能用于验收。

1. **范围** `one-shop.json`：`{"store_ids":["101"]}`。仅允许一个店铺；其在副本中须为启用。
2. **独立时区证据** `authority.json`：由有权确认统计日口径的人在截点归档。确认该店铺两个实际来源的采集全程至截点都连续使用此时区，并保存每次采集的实际开始时刻与对应的仓库完成回执。开始时刻、时区和连续有效性**不能从 SQLite 完成时间、Store 更新时间、当前配置、时差或两个恰好相等的日期推断**。请在受控档案中保留可追溯的原始依据（确认记录编号或配置版本及其生效历史、两个来源的原始采集回执），不要只保留一份手填摘要。若仅有最新配置、其中一个来源未知，或采集期间发生变动，勿事后填写。

```json
{
  "schema": "owner-pilot-zone/v1",
  "store_id": "101",
  "timezone": "America/Los_Angeles",
  "confirmed": true,
  "effective_since_utc": "2024-11-01T00:00:00Z",
  "confirmed_through_utc": "2024-11-22T06:30:00Z",
  "acquisitions": {
    "api_business_product": {
      "started_at_utc": "2024-11-22T06:20:00Z",
      "warehouse_completed_at_utc": "2024-11-22T06:22:00Z"
    },
    "api_business_finance": {
      "started_at_utc": "2024-11-22T06:24:00Z",
      "warehouse_completed_at_utc": "2024-11-22T06:26:00Z"
    }
  }
}
```

3. **截点封存记录** `capture.json`：保留与逻辑截点对应的快照及证据文件原始字节的 SHA-256（小写 64 位十六进制）。必须是当时留存的可信记录，而不是现在对旧库和新配置重新算一对哈希。

```json
{
  "schema": "owner-pilot-capture/v1",
  "store_id": "101",
  "timezone": "America/Los_Angeles",
  "cutoff_utc": "2024-11-22T06:30:00Z",
  "sqlite_sha256": "<封存 SQLite 文件摘要>",
  "evidence_sha256": "<同期独立时区证据文件摘要>"
}
```

JSON 须为 UTF-8，各不超过 16 KiB；只接受以上键，不接受重复键。两个记录内的时间须为 UTC 的 `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`。时区须为完全相同的 IANA 地区名称（如 `America/Los_Angeles`）或 `UTC`；相同偏移不能代替。每次开始/完成须在同一持续确认区间内；完成须精确对应副本中该店、该当地日、该来源的已完成非空回执。此处“非空”指成功采集至少一行，不代表金额必须大于零；有明确依据的业务零值仍保留。试点有意要求两个来源都提供佐证；一个缺失也不出卡，不尝试改用前一日或另一时区。

- **不匹配**：`PILOT_BLOCKED` 和固定原因，退出 1，无日期组和卡片。例如 `EVIDENCE_DIGEST_MISMATCH` 表示证据文件与封存摘要不符；`ACQUISITION_OUTSIDE_CONFIRMED_INTERVAL` 表示至少一次采集不在已确认区间；`ACQUISITION_RECEIPT_MISMATCH` 表示完成时刻不对应。命令行时间格式错误或未来时刻仍由参数解析器拒绝（退出 2）。
- **记录吻合**：`pilot_gate=MATCHED_OPERATOR_CAPTURE` 后才沿用[原有的分区新鲜度、币种和字段规则](OWNER_DAILY_FEISHU.md)；仅四项均为暂估值才退出 0。部分或无数值仍退出 2。不能把“记录吻合”读成“业绩确定”。
- **信任边界**：哈希核对的是字节，不是数字签名，也不证明留存时刻、记录签发人或供应端的时区。能同时改写两份记录与副本的人可以造出自洽输入。脚本也无法独立恢复分区在请求时使用的时区或验证供应商更新时间。没有可信的同期保管记录就不能做这项验收。`provider_updated_at=null`、`delivery_enabled=false`；不改变采集、早报、任务或飞书设置。
