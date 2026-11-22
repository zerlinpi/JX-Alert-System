"""Resolve collection attempt status without mistaking older facts for fresh data."""
from sqlalchemy import or_, select

from jx_alert.storage.models import SyncJob


def collection_issue_for_date(db, store_id, target, *, sales_only=False):
    """Inspect the latest attempt that covers this store, date and requested scope.

    A daily job refreshes historical advertising dates without pulling those
    dates' sales. Sales readers need the actual sales target and step outcome.
    """
    sales = SyncJob.metadata_json["report_dates"]
    ads = SyncJob.metadata_json["ad_report_dates"]
    day = target.isoformat()
    date_filters = [sales[store_id].as_string().like(f"%{day}%"), sales.as_string() == day]
    if not sales_only:
        date_filters.extend((ads[store_id].as_string().like(f"%{day}%"), ads.as_string() == day))
    jobs = db.execute(select(SyncJob.status, sales, ads, SyncJob.metadata_json["completed_steps"])
        .where(SyncJob.job_type == "daily_collection", or_(*date_filters))
        .order_by(SyncJob.id.desc()))
    required = {"sales"} if sales_only else {"sales", "campaign_reports"}
    for status, sales_dates, ad_dates, completed in jobs:
        for selected in ((sales_dates,) if sales_only else (sales_dates, ad_dates)):
            value = selected.get(store_id) if isinstance(selected, dict) else selected
            dates = value if isinstance(value, list) else [value]
            if day in dates:
                if status in {"FAILED", "PARTIAL_FAILED", "INTERRUPTED"}:
                    if (sales_only or status == "PARTIAL_FAILED") and required <= set(completed or []):
                        return "OTHER_DIMENSIONS_FAILED"
                    return "DAILY_FAILED"
                return "ATTEMPT_RECORDED"
    return None
