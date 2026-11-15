"""Resolve collection attempt status without mistaking older facts for fresh data."""
from sqlalchemy import or_, select

from jx_alert.storage.models import SyncJob


def collection_issue_for_date(db, store_id, target):
    """Inspect the latest attempt that actually covers this store and date."""
    sales = SyncJob.metadata_json["report_dates"]
    ads = SyncJob.metadata_json["ad_report_dates"]
    day = target.isoformat()
    jobs = db.execute(select(SyncJob.status, SyncJob.metadata_json["report_dates"],
        SyncJob.metadata_json["ad_report_dates"], SyncJob.metadata_json["completed_steps"])
        .where(SyncJob.job_type == "daily_collection", or_(
            sales[store_id].as_string().like(f"%{day}%"), ads[store_id].as_string().like(f"%{day}%"),
            sales.as_string() == day, ads.as_string() == day))
        .order_by(SyncJob.id.desc()))
    for status, sales_dates, ad_dates, completed in jobs:
        for selected in (sales_dates, ad_dates):
            value = selected.get(store_id) if isinstance(selected, dict) else selected
            dates = value if isinstance(value, list) else [value]
            if day in dates:
                if status == "PARTIAL_FAILED" and {"sales", "campaign_reports"} <= set(completed or []):
                    return "OTHER_DIMENSIONS_FAILED"
                if status in {"FAILED", "PARTIAL_FAILED", "INTERRUPTED"}:
                    return "DAILY_FAILED"
                return "ATTEMPT_RECORDED"
    return None
