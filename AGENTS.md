# JX-Alert-System development agreement

- Check the local work, the GitHub state, and the server revision before
  continuing. Preserve user changes when combining updates.
- Owner decision (Nov 2026): the repository keeps a single branch, `main`. Commit
  each tested update on top of `main` and push it to `zerlinpi/JX-Alert-System`
  as a fast-forward; do not leave other branches on GitHub, locally or on the
  server. Never force-push or rewrite `main`. When the owner asks to keep work
  local, say in the report that it is not pushed.
- Deploy only an identified, tested commit of `main` (or a git bundle of it when
  the work is local). Verify the commit's tree on the server, and use
  `deploy/upgrade.sh` / `deploy/rollback.sh` (backup, checks, rollback path).
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
  this repository) and let DeepSeek read them through `read_skill`. Owner decision
  (Nov 2026): install every read-only Skill of the official list; the owner ticks
  in the console which optional Skills DeepSeek uses (the required query Skills
  always stay on); Skills needing a write tool are listed with the reason but never
  installed; a run offers only the Skills whose tools SparkX offers this token.
- The system fixes each lane's scope (completed dates, monitored stores) and output
  contract around the owner's editable task text. Hourly findings must name a
  campaign ID that SparkX returned and satisfy the owner's thresholds, or they are
  dropped; each campaign/category is alerted once per marketplace day. Missing data
  stays missing, never zero. The daily report's line of stores without data is
  written by the system from that run's SparkX rows (`reports/coverage.py`), not
  by the model.
- The Feishu bot chats freely (any topic) and uses SparkX only when data is needed;
  it keeps the recent turns of one conversation. Group replies stay in the report
  group; private replies only for leads, operators and report-group members.
- The owner set every store's report timezone to `America/Los_Angeles`. Daily
  reports use the latest completed day; recent sales and orders are provisional
  (7–14 day attribution). Reviews compare the last 7 days with the 7 days before
  and judge each candidate campaign by its main product's own 30-day baseline,
  order value (the Skill's allowed spend per order T) and type (owner, Nov 2026).
- Recipients: every lane goes to the report group; the 总负责人 (one or more)
  also receive the 08:30 daily report and the 广告负责人 (one or more) the review
  and hourly alerts privately, each row labelled with its operator (the 运营
  column is always the last column). Every lane message is followed by its Excel
  copy (same content, dated, kept in `data/reports/`) to exactly the same
  recipients (owner, Nov 2026). The owner set all lanes on by default: stores without an operator
  are still notified (labelled 未映射运营, nobody @-mentioned); once operators are
  mapped, the group message @-mentions only the operators of the affected stores
  (daily: stores the report flags; review: anomalies; hourly: new alerts), naming
  their stores. Private operator copies are optional. Never route to or mention
  guessed people.
- Owner decisions (Nov 2026): SparkX's returned data and the requirements of its
  official Skills and `meta.hint` come first; JX's own task texts and output rules
  come second and give way on conflict. Texts are lean: conclusions, data and
  suggestions, with no footer or line about the data source, how the text was
  generated, query counts or the query/analysis process, and titles do not say
  "SparkX". What the Skills require readers to be told (provisional recent
  conversions, USD for multi-store money, incomplete or missing data) stays, in one
  short phrase. The bot puts an 「OK」 reaction on each question it is about to
  answer (once per question, never on one it will not answer). The console session
  is renewed while it is used (12 hours idle, 7 days at most).
- Console previews never queue anything. A test send (owner's explicit click)
  queues the server-side copy of a finished preview, marked 【测试】, under
  `jx:test:` keys and `jx_test` receipts: it never counts as a lane run or an
  hourly alert, and its recipients are re-checked like any other send.
- Re-verify the switch, delivery window and exact recipient (including each
  @-mention) before every send. Never resend a message that may have been
  submitted, and never replay old or retired messages. Lanes do not run until a
  SparkX token and a DeepSeek key are saved. Test Feishu only in the explicitly
  selected group and with the owner's recipients.
- Database changes are additive with backups and explicit persistence checks.
  Tables left by earlier releases stay untouched. Code rollback retains new
  historical evidence and never removes Docker volumes.
