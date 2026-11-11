from datetime import datetime, timezone

from sqlalchemy import select

from jx_alert.storage.models import DataCoverage


def mark_coverage(db, *, store_id: str, metric_date, dimension: str,
                  row_count: int) -> DataCoverage:
    row = db.scalar(select(DataCoverage).where(
        DataCoverage.store_id == store_id,
        DataCoverage.metric_date == metric_date,
        DataCoverage.dimension == dimension,
    ))
    if row is None:
        row = DataCoverage(
            store_id=store_id,
            metric_date=metric_date,
            dimension=dimension,
        )
        db.add(row)
    row.status = "COMPLETE"
    row.row_count = int(row_count)
    row.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return row
