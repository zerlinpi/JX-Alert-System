# JX-Alert-System development agreement

- Check the local branch and uncommitted work, the GitHub state, and the server
  revision before continuing. Preserve user changes when combining updates.
- Commit every code update on a feature branch; keep main intact unless the owner
  explicitly asks to merge. Push to `zerlinpi/JX-Alert-System` and open a PR,
  unless the owner asks to keep the work local: then keep it on a local branch
  and say in the report that it is not pushed.
- Deploy only an identified, tested commit: from GitHub, or from a git bundle of
  that commit when the work is local. Verify the commit's tree on the server, and
  use `deploy/upgrade.sh` / `deploy/rollback.sh` (backup, checks, rollback path).
- Report the exact revision/tree tested, actual test results, deployed revision,
  unresolved issues, and rollback path. Never claim unexecuted tests or deployment.
- Preserve `.env`, credentials, historical data, and Docker data volumes. Never
  print or commit secrets. Keep credentials in private 0600 files, separate from code.
- SparkX MCP is the only business data source, and access is read-only: offer only
  the tools in `sparkx.client.READ_TOOLS` that SparkX also marks
  `readOnlyHint=true`. Never offer or call write tools (`batch_update_ads`,
  `create_*`, `save_*`, `delete_*`) and never change campaigns, budgets, bids,
  listings, or store settings. Do not add other data sources, collectors or their
  credentials.
- Owner decision (Nov 2026): DeepSeek queries SparkX itself, as an MCP agent, like
  WorkBuddy/Claude with SparkX. Pass the tools exactly as SparkX lists them and the
  results unmodified (only an over-long result is cut, with a note). Install only
  the official read-only Skills from SparkXAI/SparkXAI-MCP into `data/` (never into
  this repository) and let DeepSeek read them through `read_skill`.
- The system fixes each lane's scope (completed dates, monitored stores) and output
  contract around the owner's editable task text. Hourly findings must name a
  campaign ID that SparkX returned and satisfy the owner's thresholds, or they are
  dropped; each campaign/category is alerted once per marketplace day. Missing data
  stays missing, never zero.
- The Feishu bot chats freely (any topic) and uses SparkX only when data is needed;
  it keeps the recent turns of one conversation. Group replies stay in the report
  group; private replies only for leads, operators and report-group members.
- The owner set every store's report timezone to `America/Los_Angeles`. Daily
  reports use the latest completed day; recent sales and orders are provisional
  (7–14 day attribution). Reviews compare the last 7 days with the 7 days before.
- Recipients: every lane goes to the report group; the 总负责人 also receives
  the 08:30 daily report and the 广告负责人 the review and hourly alerts
  privately, each row labelled with its operator. The owner set all lanes on by
  default: stores without an operator are still notified (labelled 未映射运营,
  nobody @-mentioned); once operators are mapped, the group message @-mentions
  only the operators of the affected stores. Private operator copies are optional.
  Never route to or mention guessed people.
- Re-verify the switch, delivery window and exact recipient (including each
  @-mention) before every send. Never resend a message that may have been
  submitted, and never replay old or retired messages. Lanes do not run until a
  SparkX token and a DeepSeek key are saved. Test Feishu only in the explicitly
  selected group and with the owner's recipients.
- Database changes are additive with backups and explicit persistence checks.
  Tables left by earlier releases stay untouched. Code rollback retains new
  historical evidence and never removes Docker volumes.
