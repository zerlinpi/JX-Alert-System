# JX-Alert-System development agreement

- Check the current GitHub main, server revision, and local uncommitted work before
  continuing. Preserve user changes when combining updates.
- Every code update must be committed and synchronized to
  `zerlinpi/JX-Alert-System` on GitHub. A local-only patch is not a finished update.
  Use a feature branch and PR; keep main intact unless the user explicitly asks
  to merge. Servers pull an identified, tested GitHub revision.
- Report the exact revision/tree tested, actual test results, deployed revision,
  unresolved issues, and rollback path. Never claim unexecuted tests or deployment.
- Preserve `.env`, credentials, historical data, and Docker data volumes. Never
  print or commit secrets. Use private credential files separately from code.
- Lingxing business operations are read-only. Do not change campaigns, budgets,
  bids, listings, or store settings. Signature changes require official evidence;
  unverified complex parameters remain rejected.
- Daily reports supply daily accounting/attribution values. Hourly Stream data
  supplies intraday observations and reconciliation. Keep revisions and observed
  differences; never manufacture balancing rows or convert missing data to zero.
- The user confirmed SID 5174 uses `America/Los_Angeles`. Analyze completed local
  dates and mature attribution windows. Do not assume this timezone applies to
  other stores or to offset-free store-health source timestamps.
- Test Feishu in the explicitly selected group. The owner authorized missing
  administrator/operator/manager mappings to fall back to the verified report
  group without unverified mentions when `feishu_group_fallback_enabled=true`.
  Keep missing identities empty; a missing/invalid group still blocks delivery.
  Do not batch replay historical failures merely because this policy changed.
- Prefer a complete seven-day baseline. The owner authorized previous-day
  comparison when seven days are unavailable and both exact adjacent dates have
  complete, explicit facts. Missing facts, failed authorization, unconfirmed
  currencies and immature advertising attribution remain protected.
- Database changes are additive with backups and explicit persistence checks.
  Code rollback retains new historical evidence and never removes Docker volumes.
- The user approved automatic health observation archiving only. Keep
  `lingxing_performance_observation_only=true`; even a FRESH source cannot create,
  update, escalate, or recover health business alerts in this mode. Data-quality
  notices are allowed. Business evaluation requires separate authorization.
