"""Durable daily read-only collection orchestration."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from jx_alert.analysis.evaluator import evaluate_store_status_events
from jx_alert.lingxing.ad_currency import account_currencies
from jx_alert.lingxing.campaign_collector import collect_campaigns
from jx_alert.lingxing.client import LingxingError, LingxingTransientError, LingxingCollectionPaused
from jx_alert.lingxing.collector import collect_ads_for_day
from jx_alert.lingxing.product_ads_collector import collect_product_ads_for_day
from jx_alert.lingxing.performance_openapi import collect_store_performance
from jx_alert.config import get_settings
from jx_alert.lingxing.report_dates import normalize_store_report_dates
from jx_alert.lingxing.sales_collector import collect_sales_for_day
from jx_alert.storage.models import RawSnapshot, SyncJob
from jx_alert.storage.raw_archive import archive_raw_snapshots
from jx_alert.store_controls import collection_states


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
    from jx_alert.lingxing.source_gateway import CollectionSources, create_collection_client
    if not isinstance(client, CollectionSources):
        client = create_collection_client(settings, api_client=client, db=db)
    elif client.db is None:
        client.db = db
    dual = isinstance(client, CollectionSources)
    stores_response = stores_response or client.get_stores()
    store_rows = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(store_rows, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")
    switches = collection_states(settings, (str(row.get("sid")) for row in store_rows))
    paused = {sid for sid, enabled in switches.items() if not enabled}
    if paused:
        store_rows = [row for row in store_rows if str(row.get("sid")) not in paused]
        stores_response = {**stores_response, "data": store_rows}
        if isinstance(report_dates, dict):
            report_dates = {sid: day for sid, day in report_dates.items() if sid not in paused}
        if not store_rows:
            return {"status": "SKIPPED_COLLECTION_DISABLED", "collection_disabled_store_ids": sorted(paused)}
    if dual:
        client.shadow_dates = {str(s["sid"]): set(normalize_store_report_dates(report_dates, str(s["sid"])))
                               for s in store_rows}
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
    source_failures = []
    if paused:
        results["collection_disabled_store_ids"] = sorted(paused)

    def archive_excel(dates=report_dates):
        from jx_alert.reporting.archive import archive_partitions
        selected = {}
        for store in store_rows:
            sid = str(store.get("sid"))
            if isinstance(dates, dict) and sid not in dates:
                continue
            selected[sid] = normalize_store_report_dates(dates, sid)
        results["excel_archive"] = archive_partitions(db, settings, selected)

    def step(name, function, dimensions=(), dates=report_dates):
        """The optional secondary source cannot erase a successful primary step."""
        try:
            value = function()
            completed_steps.append(name)
            return value
        except LingxingCollectionPaused:
            raise
        except Exception as exc:
            if not dual:
                raise
            db.rollback()
            source_failures.append({"step": name, "error_type": type(exc).__name__})
            # Keep previously valid records but hide failed exact-date primary
            # partitions. MCP observations have their own independent coverage.
            from jx_alert.storage.coverage import mark_coverage
            last = client.last_request or {}
            sid, day = last.get("store_id"), last.get("report_date")
            allowed = (set(normalize_store_report_dates(dates, sid))
                       if sid and (isinstance(dates, str) or sid in dates) else set())
            if dimensions and sid in {str(s["sid"]) for s in store_rows} and day in allowed:
                exact = dimensions
                if str(last.get("dataset", "")).startswith("product_ad_"):
                    exact = (last["dataset"],)
                for dimension in exact:
                    row = mark_coverage(db, store_id=sid, metric_date=date.fromisoformat(day),
                                        dimension=dimension, row_count=0)
                    row.status = "FAILED"
            db.commit()
            return {"status": "PARTIAL_FAILED", "rows": {}, "campaigns": {}}
        finally:
            if dual:
                client.flush(db)

    try:
        results["stores"] = {
            "count": len(stores_response.get("data", []))
            if isinstance(stores_response, dict)
            and isinstance(stores_response.get("data"), list)
            else None
        }

        if stores_response.get("_source") == "mcp_visible_cached_identity":
            # MCP visibility and cached identity are not fresh Amazon
            # authorization evidence and cannot recover or escalate events.
            results["store_status"] = {"status": "SKIPPED", "reason": "NO_FRESH_AUTHORIZATION_EVIDENCE"}
        else:
            results["store_status"] = evaluate_store_status_events(db, stores_response)
            completed_steps.append("store_status")

        results["sales"] = step("sales", lambda: collect_sales_for_day(
            client, db, report_dates, stores_response=stores_response), ("store", "sku_asin", "sku_msku"))
        from jx_alert.lingxing.business_dashboard import collect_business_for_day
        results["business_dashboard"] = collect_business_for_day(
            db, settings, stores_response, report_dates)
        if results["business_dashboard"]["status"] == "COMPLETE":
            completed_steps.append("business_dashboard")
        archive_excel()

        results["campaigns"] = step("campaigns", lambda: collect_campaigns(
            client, db, stores_response=stores_response))

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
        if dual:
            # Only the explicit target dates need the supplementary UI report;
            # the 22-day attribution refresh remains the authoritative API.
            # Collect these even if the API's earliest historical page fails.
            for sid, days in client.shadow_dates.items():
                if not any(str(s["sid"]) == sid and int(s.get("has_ads_setting") or 0) == 1
                           for s in store_rows):
                    continue
                for day in days:
                    for kind in ("sp", "sb", "sd"):
                        try:
                            client.observe_campaign(sid, day, kind)
                        except LingxingCollectionPaused:
                            raise
                        except Exception:
                            pass  # Separate MCP failed coverage is persisted below.
            client.flush(db)
            by_type = {}
            for kind in ("sp", "sb", "sd"):
                by_type[kind] = step("campaign_report_" + kind, lambda kind=kind: collect_ads_for_day(
                    client, db, ad_report_dates, stores_response=stores_response, ad_currencies=currencies,
                    report_types=(kind,)), ("campaign_" + kind,), ad_report_dates)
            results["campaign_reports"] = {"rows": {kind: result.get("rows", {}).get(kind, 0)
                for kind, result in by_type.items()}, "by_type": by_type}
            if all(value.get("status") != "PARTIAL_FAILED" for value in by_type.values()):
                completed_steps.append("campaign_reports")
        else:
            results["campaign_reports"] = collect_ads_for_day(
                client, db, ad_report_dates, stores_response=stores_response, ad_currencies=currencies)
            completed_steps.append("campaign_reports")
        archive_excel(ad_report_dates)

        if getattr(settings, "sb_sd_detail_reports_enabled", False):
            from jx_alert.lingxing.ad_details import collect_ad_details, daily_detail_dates
            results["sb_sd_detail_reports"] = collect_ad_details(
                client, db, daily_detail_dates(ad_report_dates), stores_response=stores_response,
                ad_currencies=currencies)
            completed_steps.append("sb_sd_detail_reports")

        results["product_ad_reports"] = step("product_ad_reports", lambda: collect_product_ads_for_day(
            client, db, ad_report_dates, stores_response=stores_response, ad_currencies=currencies),
            ("product_ad_sp", "product_ad_sd"), ad_report_dates)
        archive_excel(ad_report_dates)

        if settings.lingxing_performance_enabled:
            results["performance"] = collect_store_performance(
                client, db, stores_response=stores_response, settings=settings
            )
            completed_steps.append("performance")
            archive_excel()
        else:
            results["performance"] = {"status": "DISABLED", "stores_seen": 0}

        record_count = (
            int(results["sales"].get("sales_rows", 0))
            + sum(results["campaigns"].get("campaigns", {}).values())
            + sum(results["campaign_reports"].get("rows", {}).values())
            + sum(results["product_ad_reports"].get("rows", {}).values())
            + sum(results.get("sb_sd_detail_reports", {}).get("rows", {}).values())
            + int(results.get("performance", {}).get("performance_rows", 0))
            + int(results.get("performance", {}).get("notice_rows", 0))
            + int(results.get("business_dashboard", {}).get("metrics", 0))
        )
        job = db.get(SyncJob, job_id)
        performance_status = results.get("performance", {}).get("status", "DISABLED")
        detail_reports = results.get("sb_sd_detail_reports", {})
        transient_detail_failure = any(item["error_type"] == LingxingTransientError.__name__
                                       for item in detail_reports.get("failures", []))
        job.status = ("PARTIAL_FAILED" if source_failures or performance_status == "PARTIAL_FAILED" or transient_detail_failure else
                      "SUCCEEDED_WITH_WARNINGS" if performance_status == "SUCCEEDED_WITH_WARNINGS"
                      or results.get("business_dashboard", {}).get("status") == "PARTIAL_FAILED"
                      or dual and any(x.get("mcp") == "FAILED" for x in client.outcomes)
                      or detail_reports.get("status") == "PARTIAL_FAILED"
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
        if dual:
            results["sources"] = client.source_summary()
            results["source_failures"] = source_failures
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
    except LingxingCollectionPaused:
        db.rollback()
        results['raw_archive'] = archive_raw_snapshots(db, settings.raw_dir, after_id=snapshot_start_id)
        job = db.get(SyncJob, job_id)
        job.status, job.finished_at = 'SKIPPED', _utcnow()
        job.metadata_json = {'report_dates': report_dates, 'completed_steps': completed_steps,
            'results': results, 'reason': 'SKIPPED_COLLECTION_DISABLED'}
        db.commit()
        return {'job_id': job_id, 'status': 'SKIPPED_COLLECTION_DISABLED', 'steps': results}
    except Exception as exc:
        db.rollback()
        # A later provider failure must not hide already committed partitions.
        try:
            archive_excel(ad_report_dates or report_dates)
        except Exception:
            pass  # Preserve the original collection failure; disk retries are durable.
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
