# Implementation audit

## Verified in the repository

- Sales, SP/SB/SD campaign reports and SP/SD product-ad reports have persisted collectors and coverage-aware evaluation.
- Feishu routing and persistent outbox exist; custom-bot delivery is at-least-once.
- Store performance list/detail and performance notices use the three confirmed read-only OpenAPI paths.
- Performance list, detail and notice responses are retained as database RawSnapshot JSON.
- Performance freshness is separate from request time; missing timezone, missing source timestamp, stale data and failed calls remain explicit states.
- SB creative association is not SB product-level measured spend. SB spend allocation is not treated as actual ASIN spend.

## Not yet production-verified

- Confirm the source timezone for update_date / lastUpdateDate and set it on the server.
- Validate live response fields, account authorization and pagination with a sanitized production response.
- Independent immutable filesystem JSON archival is not part of the restored collector; raw JSON is persisted in the database.
- SB product-level measured spend remains unavailable until a documented endpoint and valid sanitized response establish its contract.
- Docker deployment and real Feishu delivery have not been run against the Linux server in this session.
