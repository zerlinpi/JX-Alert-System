# Collection, notifications and rollback

All changes are developed on a feature branch based on upstream `4256f30`.
Keep `main` intact until review. Pull the feature branch on the server before
changing the running source. Never copy credentials or the `data/` directory
into Git. Existing `.env` is retained.

## Persistent data and source updates

`init_db()` creates six additional SP version tables. It does not rename or drop
existing tables. `stores`, `campaigns`, `anomaly_events`, `sync_jobs`, and the
notification outbox remain the existing sources for their respective entities.
Use `latest_sp_partition()` for totals: summing every version would count the
same report repeatedly. Money uses `NUMERIC(32,12)` in the SP tables.

Back up MariaDB before upgrades. `docker-compose.runtime.yml` mounts the checked
out `src/` read-only and reuses the existing `jx-alert-app` image for application,
scheduler and MCP. It does not rebuild the image or replace the MariaDB volume.
Start with the existing compose file plus this override, with `--no-build`.
Only activate the optional scheduler/MCP profiles after validating configuration.

Runtime flags and non-sensitive paths live in `/app/data/runtime-settings.json`.
Only explicitly allowed fields can be overridden. Database credentials are not
accepted there. Store scope, verified marketplace timezones, selected SP targets,
and Feishu routing belong in separate configuration files under `data/`.
Feishu app credentials and MCP authentication belong in mode-0600 files under
`data/secrets/`; never put their contents in logs, command output, or Git.

## Daily jobs and manual backfill

The scheduler and manual jobs share `run_collection()` and a connection-scoped
MariaDB advisory lock. A crashed process releases its lock. Manual requests are
persistent jobs with bounded retries; unfinished dispatches are recovered under
the dispatcher lock. Explicit idempotency keys reuse previous requests.

```bash
PYTHONPATH=src python scripts/run_pipeline.py
PYTHONPATH=src python scripts/run_pipeline.py --collect --backfill-days 22
```

The first command is a dry run. Collection also respects `APP_DRY_RUN` (or its
runtime flag). The configured store scope is checked before business requests.
Only completed marketplace-local dates can enter the analysis pipeline. SP
conversion-dependent rules additionally use their existing mature attribution
window. Current-day SP evidence may be inspected separately, but is never
substituted for authoritative daily reports or mature conversion baselines.

`request_collection` in MCP requires `mcp_operations_enabled` and returns a job
ID immediately. A running scheduler executes the request. SP query tools expose
separate daily/hour versions, stale-after-failure indicators and reconciliations.
Keep MCP bound to loopback unless an authenticated HTTPS reverse proxy is ready.

## Feishu testing and ownership

The application bot sends to an explicitly configured test/report group. Daily
snapshots are deduplicated by store, local date and destination. Group anomaly
alerts require both administrator and operator mappings; missing mappings become
`ROUTING_FAILED`, never guessed recipients. Management DMs are independent
deliveries. An unresolved operator does not prevent a daily test snapshot.

The outbox retries with a persistent delay, records provider message IDs, and
prevents a stale worker from overwriting a newer result. API UUIDs are reused
for retries. Delivery remains at-least-once across a crash after provider
acceptance but before the database commit; exact-once delivery is not claimed.
`process_outbox_batch(outbox_ids=[...])` can isolate a test delivery from other
pending messages. Repairing a failed route does not resend successful recipients.

On the current server, edit
`/opt/jx-alert-system/data/sp-runtime/feishu-routing.json`:

- `groups.test.admin_open_ids`: administrator Open IDs.
- `stores.5174.open_ids`: the store's operator Open IDs.
- `management.open_ids`: recipients of independent management DMs.

Each is an array of real `ou_...` identifiers. Leave unknown recipients empty.
The group destination is already configured; credentials are stored separately.
See `config/feishu-app-routing.example.json` for the non-sensitive structure.

## Store health source validation

Live read-only checks accepted a store-health list page of 200, but the separate
performance-notice endpoint rejected 200 with provider code 400 and accepted 100.
The collector caps only notice pages at the verified size of 100 and still reads
every page. This is an observed compatibility limit, not a documented maximum.
Health collection can run in observation mode before the source timestamps'
timezone is confirmed. Unknown, missing, old or future source times block both
new health-risk alerts and recovery of old ones while retaining observations.
Only data-quality messages may change then. Complete requests with quality
warnings return `SUCCEEDED_WITH_WARNINGS`; the manual dispatcher records that
state without repeating a successful full collection. Request failures retain
failure/retry behavior. Freshness must be verified before health-risk evaluation.

## Historical rule replay

`analysis.backtest.backtest_history()` copies the selected store's historical
facts into an isolated in-memory database and calls the same production
evaluator. Its anomaly and outbox rows never enter the source warehouse, and
no worker sends them. Export returned candidates with review labels for manual
assessment. Without human true/false-positive labels the false-positive rate
is `null`, not zero. Current revised historical data is replayed; this is not
a reconstruction of what was known at every past collection time.

## Rollback

1. Stop the scheduler/MCP processes to stop new jobs; retain outbox and job data.
2. Switch the server checkout to the recorded previous commit (`4256f30` for this
   upgrade), or disable the source override and use the previous installed image.
3. Restart only the application processes using the same data/database volumes.
4. Keep the six additive SP tables and their evidence for investigation. The old
   code ignores them. Do not drop tables or run `docker compose down -v`.
5. Recheck application health and stored row counts. Restore a database backup
   only when data restoration is specifically needed; code rollback does not
   require removing new historical evidence.

SB/SD campaign and SD product report adapters already exist in the shared daily
pipeline. SB creative links are not product performance and must not be used to
allocate or invent per-ASIN spend. Store-health collection still requires a
verified source timestamp timezone and freshness checks before it is enabled.
