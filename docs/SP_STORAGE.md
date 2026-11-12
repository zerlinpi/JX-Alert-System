# SP report storage and version selection

The SP persistence layer adds six tables without changing, replacing, or
dropping existing tables. It uses the existing `stores`, `campaigns`,
`raw_snapshots`, `sync_jobs`, `anomaly_events`, and notification tables for
their existing purposes. `anomaly_events` remains the alert record table;
there is no duplicate `alert_records` table.

| New table | Grain and purpose |
| --- | --- |
| `collection_logs` | One immutable collection run, capture interval, status, content hash, normalized counts, and complete summary |
| `sp_daily_reports` | One campaign/report date per version; includes all campaigns returned for the requested store |
| `sp_hourly_reports` | One campaign/hour or ad group/hour per version, separated by `scope` |
| `sp_advertise_details` | One advertised product/hour per version, with ad ID, group ID, ASIN, and seller SKU when supplied |
| `sp_target_details` | One target/hour per version, with target ID, expression, and match type when supplied |
| `reconciliation_logs` | One date/scope per run, with collection status, observed daily/hour values and differences |

The daily scope also has a `reconciliation_logs` row with comparison status
`NOT_APPLICABLE`. This row records successful empty or unmatched partitions
even when there are no fact rows. The other four scope rows retain their actual
`EQUAL`, `DIFFERENT`, `SCOPE_MISMATCH`, or `UNAVAILABLE` comparison status.

## Capture and transaction boundaries

Import `jx_alert.storage.sp` before calling the existing database initialization
so its tables are registered with SQLAlchemy. On MariaDB the new tables use
InnoDB and utf8mb4. Monetary values use `DECIMAL(32,12)` and counts use
`BIGINT`; floating point is not used to persist cost or sales.

```python
from jx_alert.lingxing.sp_validation import collect_sp_validation
from jx_alert.lingxing.sp_persistence import persist_sp_validation

result = collect_sp_validation(
    client, db, sid=5174, campaign_id=141469378857743,
    report_dates=["2026-11-11"],
)
stored = persist_sp_validation(db, result)
db.commit()
```

Neither service commits the caller's transaction. Raw snapshots and relational
facts therefore remain under the caller's control. A savepoint makes each new
relational run atomic; a validation failure cannot leave a half-written version.
Previously committed versions are never deleted. Failed collection partitions
retain logs but create no valid fact rows.

Persistence reads the original snapshot pages for the run, validates the store,
campaign, date, total, identity, and numeric fields again, and checks their
normalized result against the summary. A changed summary cannot replace raw
response values. Only already redacted raw row JSON is retained.

## Replays, backfills, and reading totals

The same run ID and content is idempotent. Reusing a run ID with different
content is rejected. Recollecting the same dates with a new run ID appends a
new version. Unique run/grain keys prevent duplicate writes from accumulating
spend.

**Do not sum fact tables across all runs.** Use `latest_sp_partition` to select
one complete version for a store, target campaign, date, and scope:

```python
from jx_alert.lingxing.sp_persistence import latest_sp_partition

partition = latest_sp_partition(
    db, sid=5174, campaign_id=141469378857743,
    report_date="2026-11-11", scope="daily",
)
```

It returns the selected run ID, collection/comparison statuses, ORM fact rows,
and Decimal totals for that one scope. The campaign must be the target of the
collection run; a daily collection still stores every campaign returned by
the store-wide endpoint. Hourly scopes are `campaign`, `ad_group`, `ad`, and
`target`. They are alternate dimensions and must never be added together.

A later failed attempt does not replace an older valid partition. The reader
then returns `stale_after_failure=True` and the latest attempt's status so an
analysis or notification caller can decline to use stale data as current.
With no earlier successful version the result is `UNAVAILABLE`, with no
numeric totals. A successful empty or unmatched version supersedes older
rows and also keeps totals absent; only an explicit matched row can represent
real zero spend.

The daily endpoint remains the daily reporting source. Hourly Stream data is
stored separately for intraday observation and reconciliation. A difference
never changes daily facts or creates an artificial balancing row. Source
updates are recorded through another append-only backfill.

## Deployment and rollback

Back up the existing database before enabling additive schema creation.
Deploy code that imports these models and verify the six new tables before
running collection. Check the actual MariaDB backend, transaction behavior,
and container volume persistence in the deployment environment; SQLite tests
alone are not production MariaDB acceptance.

To roll back code, restore the previous application image and source revision.
The previous application ignores these new tables, so preserve them and all
historical data. A code rollback does not require dropping tables or restoring
an older database backup over newly collected user data.
