from datetime import datetime, timezone
import math

from sqlalchemy import select

from jx_alert.storage.models import DataCoverage


SALES_DATA_LABELS = {
    "AUTHORIZATION_FAILED": "授权失败",
    "STORE_UNAVAILABLE": "店铺状态待核实",
    "COLLECTION_FAILED": "采集失败",
    "NOT_COLLECTED": "尚未采集",
    "EMPTY": "接口无记录",
    "PARTIAL": "数据不完整",
    "ZERO": "真实零",
    "AVAILABLE": "有记录",
}


def is_empty_store_partition(record, facts, asin_record=None, msku_record=None):
    """Identify an empty store partition without inventing a zero aggregate."""
    if record is None or record.status != "COMPLETE" or facts:
        return False

    def empty_grain(row, dimension):
        return (row.store_id == record.store_id
                and row.metric_date == record.metric_date and row.dimension == dimension
                and row.status == "COMPLETE" and row.row_count == 0)

    grains = ((asin_record, "sku_asin"), (msku_record, "sku_msku"))
    if record.row_count == 0:
        # An ASIN aggregate is absent if ASIN returns no rows. MSKU
        # evidence can still exist, which makes this a partial result.
        return all(row is None or empty_grain(row, dimension) for row, dimension in grains)
    # Older collectors always wrote one store-level row to coverage, even
    # when both underlying report grains had no rows. Require both records.
    return record.row_count == 1 and all(row is not None and empty_grain(row, dimension)
                                        for row, dimension in grains)


def store_sales_data_status(store_status, record, facts, asin_record=None, msku_record=None, *, issue=None):
    """Classify target-date sales evidence before comparing its values."""
    if store_status == 2:
        return "AUTHORIZATION_FAILED"
    if store_status != 1:
        return "STORE_UNAVAILABLE"
    if issue == "DAILY_FAILED" or (record is not None and record.status in {"FAILED", "PARTIAL_FAILED", "INTERRUPTED"}):
        return "COLLECTION_FAILED"
    if record is None:
        return "PARTIAL" if facts else "NOT_COLLECTED"
    if is_empty_store_partition(record, facts, asin_record, msku_record):
        return "EMPTY"
    values = {row.metric_name: row.value for row in facts if math.isfinite(row.value) and row.value >= 0}
    required = {"orders", "units", "sales"}
    if record.status != "COMPLETE" or record.row_count > 1 or not required <= values.keys():
        return "PARTIAL"
    return "ZERO" if all(values[name] == 0 for name in required) else "AVAILABLE"


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
