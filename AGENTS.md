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
- SparkX MCP is the only business data source, and access is read-only: call only
  `get_user_authorized_context`, `get_ads_perf` and `get_entity_metadata`. Never
  change campaigns, budgets, bids, listings, or store settings. Lingxing is
  retired; do not reintroduce its collectors or credentials.
- Follow the official SparkXAI-MCP skill documentation for query shapes; do not
  guess undocumented fields or parameters.
- Every figure in a notification or answer comes from SparkX rows and is computed
  in code. Missing rows or fields stay missing, never zero. Incomplete paging,
  unconfirmed or mixed currencies, and duplicate or out-of-scope rows fail the run
  instead of producing partial numbers.
- DeepSeek only rephrases computed facts. Text containing a figure, date or store
  that is not in the facts is rejected and replaced by rule-based text (or by the
  raw SparkX result for questions).
- The owner set every store's report timezone to `America/Los_Angeles`. Daily
  reports use the latest completed day; recent sales and orders are provisional
  (7–14 day attribution). Reviews compare the last 7 days with the 7 days before.
- Recipients: the 总负责人 receives the 08:30 daily report; the 广告负责人
  receives the review and hourly alerts for all stores, each row labelled with its
  operator; each operator receives only their own stores. Unmapped stores are
  labelled, not routed to guessed people.
- Re-verify the switch, delivery window and exact recipient before every send.
  Never resend a message that may have been submitted, and never replay old or
  retired messages. Lanes stay off until the owner enables them in the console.
  Test Feishu only in the explicitly selected group and with the owner's recipients.
- Database changes are additive with backups and explicit persistence checks.
  Retired Lingxing tables stay untouched. Code rollback retains new historical
  evidence and never removes Docker volumes.
