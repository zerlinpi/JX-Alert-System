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
- Test Feishu in the explicitly selected group. Unknown administrator, operator,
  or manager mappings remain empty and block affected anomaly deliveries.
- Database changes are additive with backups and explicit persistence checks.
  Code rollback retains new historical evidence and never removes Docker volumes.
