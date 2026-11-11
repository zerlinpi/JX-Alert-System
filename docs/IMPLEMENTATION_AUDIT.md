# Implementation audit

## Implemented and CI-verified

- Sales, SP/SB/SD campaign reports, SP/SD product-ad reports, campaign/product master data, coverage-aware evaluation and daily scheduling are integrated.
- Feishu routing, persistent outbox, retry handling and delivery logs are implemented. Custom-bot delivery is at-least-once.
- Store performance list/detail and performance notice collection use the three read-only Lingxing OpenAPI paths.
- Performance scores, source indicators, provider standards, breaches, notice states and source freshness are persisted.
- Missing, stale, future, failed or timezone-unconfigured performance data is not classified as healthy.
- Database RawSnapshot records created during daily collection are also archived as atomic JSON files under RAW_DIR/YYYY/MM/DD. Archive directories use mode 0700 and files use mode 0600; snapshot IDs make writes idempotent.
- Store performance and pending-notice summaries are included in the Excel report.
- SB creative associations and spend-allocation outputs are not treated as measured ASIN-level spend.

## Automated validation

- GitHub Actions on the merged main commit passed.
- Ruff passed.
- Pytest: 88 passed.

## Still requires production validation

- Confirm the actual source timezone for update_date / lastUpdateDate and set LINGXING_PERFORMANCE_SOURCE_TIMEZONE.
- Verify real account authorization, exact live response fields, pagination and standard mappings with sanitized production responses.
- Performance collection is disabled by default (LINGXING_PERFORMANCE_ENABLED=false) until the preceding checks are complete.
- Verify Docker/MariaDB startup, daily scheduler operation, raw archive persistence on the Linux host, real Feishu group routing/@mentions and remote MCP access on the user's server.
- SB product-level measured spend remains unavailable until a documented endpoint and valid sanitized response establish its contract.
