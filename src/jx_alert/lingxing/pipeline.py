"""Durable daily read-only collection orchestration."""
from datetime import datetime, timezone

from jx_alert.lingxing.campaign_collector import collect_campaigns
from jx_alert.lingxing.collector import collect_ads_for_day
from jx_alert.lingxing.product_ads_collector import collect_product_ads_for_day
from jx_alert.lingxing.sales_collector import collect_sales_for_day
from jx_alert.storage.models import SyncJob


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def collect_daily_snapshot(client, db, report_dates: str | dict[str, str]) -> dict:
    """Collect sales, campaign masters, campaign ads and SP/SD product ads.

    Each collector is idempotent for its store/date grain. A failed later step
    leaves earlier completed steps persisted and marks the SyncJob PARTIAL_FAILED;
    a retry can safely replace the same daily metric partitions.
    """
    job = SyncJob(
        job_type="daily_collection",
        status="RUNNING",
        started_at=_utcnow(),
        metadata_json={"completed_steps": []},
    )
    db.add(job)
    db.commit()
    job_id = job.id
    completed_steps: list[str] = []
    results: dict = {}

    try:
        stores_response = client.get_stores()
        results["stores"] = {
            "count": len(stores_response.get("data", []))
            if isinstance(stores_response, dict)
            and isinstance(stores_response.get("data"), list)
            else None
        }

        results["sales"] = collect_sales_for_day(
            client, db, report_dates, stores_response=stores_response
        )
        completed_steps.append("sales")

        results["campaigns"] = collect_campaigns(
            client, db, stores_response=stores_response
        )
        completed_steps.append("campaigns")

        results["campaign_reports"] = collect_ads_for_day(
            client, db, report_dates, stores_response=stores_response
        )
        completed_steps.append("campaign_reports")

        results["product_ad_reports"] = collect_product_ads_for_day(
            client, db, report_dates, stores_response=stores_response
        )
        completed_steps.append("product_ad_reports")

        record_count = (
            int(results["sales"].get("sales_rows", 0))
            + sum(results["campaigns"].get("campaigns", {}).values())
            + sum(results["campaign_reports"].get("rows", {}).values())
            + sum(results["product_ad_reports"].get("rows", {}).values())
        )
        job = db.get(SyncJob, job_id)
        job.status = "SUCCEEDED"
        job.finished_at = _utcnow()
        job.store_count = int(results["sales"].get("stores_seen", 0))
        job.record_count = record_count
        job.metadata_json = {
            "report_dates": report_dates,
            "completed_steps": completed_steps,
            "results": results,
        }
        db.commit()
        return {
            "job_id": job_id,
            "status": "SUCCEEDED",
            "store_count": job.store_count,
            "record_count": record_count,
            "steps": results,
        }
    except Exception as exc:
        db.rollback()
        job = db.get(SyncJob, job_id)
        if job is not None:
            job.status = "PARTIAL_FAILED" if completed_steps else "FAILED"
            job.finished_at = _utcnow()
            job.error_message = f"{type(exc).__name__}: {str(exc)[:1500]}"
            job.metadata_json = {
                "report_dates": report_dates,
                "completed_steps": completed_steps,
                "results": results,
            }
            db.commit()
        raise
