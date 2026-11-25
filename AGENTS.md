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
- SparkX MCP is the only business data source. Scheduled lanes, console previews
  and test sends only read: offer them only the tools in `sparkx.client.READ_TOOLS`
  that SparkX also marks `readOnlyHint=true`. Do not add other data sources,
  collectors or their credentials.
- Owner decision (Nov 2026, replaces the earlier read-only rule): ads may be changed
  only through the Feishu bot's write mode, when the console's 「广告写入」 switch
  (`data/sparkx-write.json`, off by default and off when unreadable) is on and a
  总负责人 or 广告负责人 asks. Only then does DeepSeek also get the tools in
  `sparkx.client.WRITE_TOOLS` and every official write Skill. The worker, never the
  model, decides the turn: a plan turn blocks every write call; writes run only in
  the turn where the same lead, in the same conversation, answered exactly
  「确认执行」 to that plan (at most 30 minutes old), at most `agent.MAX_WRITES`
  calls, and an interrupted execute turn is never run again. Every attempt is kept
  as a `sparkx_write` row without the confirmToken. SparkX's skill-platform tools
  are never offered. Never call a write tool against live SparkX in development or
  tests (use fakes).
- Owner decision (Nov 2026): DeepSeek queries SparkX itself, as an MCP agent, like
  WorkBuddy/Claude with SparkX. Pass the tools exactly as SparkX lists them and the
  results unmodified (only an over-long result is cut, with a note). Install the
  official Skills from SparkXAI/SparkXAI-MCP into `data/` (never into this
  repository) and let DeepSeek read them through `read_skill`: every read Skill and
  every write Skill whose tools this release knows (a Skill needing any other tool
  is listed with the reason, never installed). The owner ticks in the console which
  optional read Skills DeepSeek uses (the required query Skills always stay on);
  write Skills cannot be ticked one by one and are offered only in write mode; a
  run offers only the Skills whose tools SparkX offers this token.
- The system fixes each lane's scope (completed dates, monitored stores) and output
  contract around the owner's editable task text. Hourly findings must name a
  campaign ID that SparkX returned and satisfy the owner's thresholds, or they are
  dropped; each campaign/category is alerted once per marketplace day. Missing data
  stays missing, never zero. The daily report's line of stores without data is
  written by the system from that run's SparkX rows (`reports/coverage.py`), not
  by the model.
- The Feishu bot chats freely (any topic) and uses SparkX only when data is needed;
  it keeps the recent turns of one conversation. Group replies stay in the report
  group; private replies only for leads, operators and report-group members. It
  changes ads only in write mode, as described above.
- The owner set every store's report timezone to `America/Los_Angeles`. Daily
  reports use the latest completed day; recent sales and orders are provisional
  (7–14 day attribution). Reviews compare the last 7 days with the 7 days before
  and judge each candidate campaign by its main product's own 30-day baseline,
  order value (the Skill's allowed spend per order T) and type (owner, Nov 2026).
  Review suggestions keep their numeric bids for campaigns outside managed groups
  (owner, Nov 2026); managed-group campaigns get group-level advice only.
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
  queues the server-side copy of a finished preview under `jx:test:` keys and
  `jx_test` receipts, with no 测试 wording in its title or Excel name (owner, Nov
  2026; the console still labels it): it never counts as a lane run or an hourly
  alert, and its recipients are re-checked like any other send.
- Re-verify the switch, delivery window and exact recipient (including each
  @-mention) before every send. Never resend a message that may have been
  submitted, and never replay old or retired messages. Lanes do not run until a
  SparkX token and a DeepSeek key are saved. Test Feishu only in the explicitly
  selected group and with the owner's recipients.
- Database changes are additive with backups and explicit persistence checks.
  Tables left by earlier releases stay untouched. Code rollback retains new
  historical evidence and never removes Docker volumes.
