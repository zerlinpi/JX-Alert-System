"""Durable daily read-only collection orchestration."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from jx_alert.analysis.evaluator import evaluate_store_status_events
from jx_alert.lingxing.ad_currency import account_currencies
from jx_alert.lingxing.campaign_collector import collect_campaigns
from jx_alert.lingxing.client import LingxingError
from jx_alert.lingxing.collector import collect_ads_for_day
from jx_alert.lingxing.product_ads_collector import collect_product_ads_for_day
from jx_alert.lingxing.performance_openapi import collect_store_performance
from jx_alert.config import get_settings
from jx_alert.lingxing.report_dates import normalize_store_report_dates
from jx_alert.lingxing.sales_collector import collect_sales_for_day
from jx_alert.storage.models import RawSnapshot, SyncJob
from jx_alert.storage.raw_archive import archive_raw_snapshots


def ad_account_inventory(client):
    if not hasattr(client, "seller_ad_accounts"):
        return None
    try:
        return client.seller_ad_accounts()
    except LingxingError:
        return None


_LOAD_AD_ACCOUNTS = object()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def collect_daily_snapshot(client, db, report_dates: str | dict[str, str], *,
                           stores_response: dict | None = None,
                           ad_attribution_refresh_days: int = 22,
                           settings=None, ad_accounts=_LOAD_AD_ACCOUNTS) -> dict:
    """Collect sales, campaign masters, campaign ads and SP/SD product ads.

    Each collector is idempotent for its store/date grain. A failed later step
    leaves earlier completed steps persisted and marks the SyncJob PARTIAL_FAILED;
    a retry can safely replace the same daily metric partitions.
    """
    if not 1 <= ad_attribution_refresh_days <= 31:
        raise ValueError("ad_attribution_refresh_days must be between 1 and 31")
    settings = settings or get_settings()
    stores_response = stores_response or client.get_stores()
    store_rows = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(store_rows, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")
    ad_report_dates = {}
    for store in store_rows:
        if int(store.get("status") or 0) != 1 or int(store.get("has_ads_setting") or 0) != 1:
            continue
        sid = store.get("sid")
        if sid is None:
            raise RuntimeError("Active ad store is missing sid")
        latest_dates = normalize_store_report_dates(report_dates, str(sid))
        latest_date = date.fromisoformat(latest_dates[-1])
        ad_report_dates[str(sid)] = [
            (latest_date - timedelta(days=offset)).isoformat()
            for offset in range(ad_attribution_refresh_days - 1, -1, -1)
        ]

    snapshot_start_id = db.scalar(select(func.max(RawSnapshot.id))) or 0

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
        results["stores"] = {
            "count": len(stores_response.get("data", []))
            if isinstance(stores_response, dict)
            and isinstance(stores_response.get("data"), list)
            else None
        }

        results["store_status"] = evaluate_store_status_events(db, stores_response)
        completed_steps.append("store_status")

        results["sales"] = collect_sales_for_day(
            client, db, report_dates, stores_response=stores_response
        )
        completed_steps.append("sales")

        results["campaigns"] = collect_campaigns(
            client, db, stores_response=stores_response
        )
        completed_steps.append("campaigns")

        results["ad_report_dates"] = ad_report_dates
        currencies = {}
        if ad_report_dates:
            accounts = ad_account_inventory(client) if ad_accounts is _LOAD_AD_ACCOUNTS else ad_accounts
            if accounts is not None:
                currencies = account_currencies(accounts)
                if ad_accounts is _LOAD_AD_ACCOUNTS:
                    db.add(RawSnapshot(source="lingxing_seller_ad_accounts", payload={"rows": accounts}))
            results["ad_currency"] = {"status": "COMPLETE" if currencies else "LOOKUP_UNAVAILABLE",
                                      "accounts_matched": len(currencies)}
        results["campaign_reports"] = collect_ads_for_day(
            client, db, ad_report_dates, stores_response=stores_response, ad_currencies=currencies,
        )
        completed_steps.append("campaign_reports")

        results["product_ad_reports"] = collect_product_ads_for_day(
            client, db, ad_report_dates, stores_response=stores_response, ad_currencies=currencies,
        )
        completed_steps.append("product_ad_reports")

        if settings.lingxing_performance_enabled:
            results["performance"] = collect_store_performance(
                client, db, stores_response=stores_response, settings=settings
            )
            completed_steps.append("performance")
        else:
            results["performance"] = {"status": "DISABLED", "stores_seen": 0}

        record_count = (
            int(results["sales"].get("sales_rows", 0))
            + sum(results["campaigns"].get("campaigns", {}).values())
            + sum(results["campaign_reports"].get("rows", {}).values())
            + sum(results["product_ad_reports"].get("rows", {}).values())
            + int(results.get("performance", {}).get("performance_rows", 0))
            + int(results.get("performance", {}).get("notice_rows", 0))
        )
        job = db.get(SyncJob, job_id)
        performance_status = results.get("performance", {}).get("status", "DISABLED")
        job.status = ("PARTIAL_FAILED" if performance_status == "PARTIAL_FAILED" else
                      "SUCCEEDED_WITH_WARNINGS" if performance_status == "SUCCEEDED_WITH_WARNINGS"
                      or results.get("ad_currency", {}).get("status") == "LOOKUP_UNAVAILABLE" else "SUCCEEDED")
        job.finished_at = _utcnow()
        job.store_count = int(results["sales"].get("stores_seen", 0))
        job.record_count = record_count
        job.metadata_json = {
            "report_dates": report_dates,
            "ad_report_dates": ad_report_dates,
            "ad_attribution_refresh_days": ad_attribution_refresh_days,
            "completed_steps": completed_steps,
            "results": results,
        }
        db.commit()
        results["raw_archive"] = archive_raw_snapshots(
            db, settings.raw_dir, after_id=snapshot_start_id
        )
        completed_steps.append("raw_archive")
        job = db.get(SyncJob, job_id)
        job.metadata_json = {
            **(job.metadata_json or {}),
            "completed_steps": completed_steps,
            "results": results,
        }
        db.commit()
        return {
            "job_id": job_id,
            "status": job.status,
            "store_count": job.store_count,
            "record_count": record_count,
            "steps": results,
        }
    except Exception as exc:
        db.rollback()
        archive_error = None
        try:
            results["raw_archive"] = archive_raw_snapshots(
                db, settings.raw_dir, after_id=snapshot_start_id
            )
        except Exception as archive_exc:
            archive_error = f"raw archive: {type(archive_exc).__name__}: {str(archive_exc)[:500]}"
        job = db.get(SyncJob, job_id)
        if job is not None:
            job.status = "PARTIAL_FAILED" if completed_steps else "FAILED"
            job.finished_at = _utcnow()
            error_text = f"{type(exc).__name__}: {str(exc)[:1200]}"
            if archive_error:
                error_text = f"{error_text}; {archive_error}"
            job.error_message = error_text[:1500]
            job.metadata_json = {
                "report_dates": report_dates,
                "ad_report_dates": ad_report_dates,
                "ad_attribution_refresh_days": ad_attribution_refresh_days,
                "completed_steps": completed_steps,
                "results": results,
                "raw_archive_error": archive_error,
            }
            db.commit()
        raise
