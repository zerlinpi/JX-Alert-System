# Store performance collection

Read-only endpoints:

- POST /basicOpen/customerService/storeTarget/list
- POST /basicOpen/customerService/storeTarget/detail
- POST /basicOpen/customerService/performanceNotice/list

The list is queried one store at a time with sids, offset and length. Detail uses the latest pull_date as pullDate. Notice collection intentionally omits status, read-state and date filters so pending notices are not hidden.

## Freshness

The performance list update_date and notice data.lastUpdateDate are source timestamps, not request timestamps. They have no explicit UTC offset. Set LINGXING_PERFORMANCE_SOURCE_TIMEZONE only after confirming the source timezone. Until then freshness is NOT_CONFIGURED, not healthy. Missing timestamps are MISSING; old data is STALE; future data is FUTURE. Default maximum age is 8 hours.

## Notice status

- 1: PENDING
- 2: PROCESSED
- 3: NO_ACTION
- 0 or unknown: UNKNOWN

Only explicit 2 or 3 closes a pending notice. Status 0 is not handled. Provider IDs are used for notice identity; otherwise a SHA-256 digest is used.

## Standards and raw evidence

Detail data.standard is persisted and used only when numeric actual values and provider standards are both present. Missing markers such as '-' are not zero. A performance scope only recovers old anomalies when source data is fresh, the health status is recognized, and standards cover every returned comparable indicator. Every list/detail/notice page is retained as a database JSON RawSnapshot and independently archived to the filesystem by the daily pipeline.

Set LINGXING_PERFORMANCE_SOURCE_TIMEZONE after verifying the source timezone. Keep it blank until then.

## Independent filesystem archive

Each new RawSnapshot created by the daily collection run is also written under `RAW_DIR/YYYY/MM/DD/<source>/<store>/` as a UTF-8 JSON file. The write is atomic (temporary file followed by rename), file permissions are restricted to owner read/write (0600), and the snapshot ID makes the archive idempotent. Database RawSnapshot rows remain the queryable source of truth; the filesystem copy is the independent retention/archive layer. Set `RAW_DIR=/app/data/raw`; Docker Compose mounts `./data:/app/data` for persistence.
