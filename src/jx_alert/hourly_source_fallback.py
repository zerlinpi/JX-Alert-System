"""Preserve independent MCP observations when the hourly OpenAPI source fails."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from jx_alert.lingxing.api_business import collect_api_business_for_day
from jx_alert.lingxing.business_dashboard import collect_business_for_day
from jx_alert.lingxing.intraday_observation import run_observation
from jx_alert.lingxing.performance_openapi import collect_store_performance
from jx_alert.lingxing.source_credentials import mcp_credentials
from jx_alert.lingxing.source_gateway import CollectionSources
from jx_alert.notifications.business_daily import DIMENSION
from jx_alert.operations import _checked_scope, store_report_plan
from jx_alert.reporting.archive import archive_partitions
from jx_alert.storage.models import DataCoverage, RawSnapshot
from jx_alert.storage.raw_archive import archive_raw_snapshots
from jx_alert.store_controls import collection_states


def _utcnow():
    return datetime.now(timezone.utc)


def _business_dates(db, result, targets):
    dates = {}
    for item in result.get("partitions", []):
        sid = item["store_id"]
        day = targets.get(sid)
        if not day or day not in item["dates"]:
            continue
        coverage = db.scalar(select(DataCoverage).where(DataCoverage.store_id == sid,
            DataCoverage.dimension == DIMENSION, DataCoverage.metric_date == date.fromisoformat(day)))
        if coverage and coverage.status == "COMPLETE" and coverage.row_count > 0:
            dates[sid] = day
    return dates


def collect_fallback_observations(db, settings, *, now, morning=False):
    """Collect MCP-only observations for previously authorized, currently visible stores.

    The caller keeps the OpenAPI failure and retry state. This path cannot
    establish ad state, populate attribution facts or queue any notifications.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Hourly fallback requires an aware clock")
    result = {"status": "NO_ELIGIBLE_STORES", "morning_dates": {}, "observations": {}, "failures": []}
    scope = settings.collection_scope_file
    if not Path(scope).is_file():
        return {**result, "status": "NO_EXPLICIT_SCOPE"}
    # Validate the explicit scope before invoking even a read-only provider.
    allowed = _checked_scope(settings, None)
    if mcp_credentials(settings) is None:
        return {**result, "status": "NOT_CONFIGURED"}

    before = db.scalar(select(func.max(RawSnapshot.id))) or 0
    client = CollectionSources(None, settings, db=db, observation_mode=True)
    try:
        inventory = client.get_stores()
    except Exception as exc:
        db.rollback()
        result["status"] = "PARTIAL_FAILED"
        result["failures"].append({"step": "inventory", "error_type": type(exc).__name__})
        client.flush(db, raw_only=True)
        result["raw_archive"] = archive_raw_snapshots(db, settings.raw_dir, after_id=before)
        return result
    # The gateway checks the official MCP store list and returns only existing
    # identities with their last API authorization status intact.
    rows = [row for row in inventory["data"] if str(row["sid"]) in allowed]
    states = collection_states(settings, [str(row["sid"]) for row in rows])
    rows = [row for row in rows if int(row.get("status") or 0) == 1 and states[str(row["sid"])]]
    plan = {row["store_id"]: row for row in store_report_plan(rows, settings.store_timezones_file,
        now=now, allow_unknown_timezone_history=settings.allow_unconfigured_timezone_history)
        if row["state"] == "READY"}
    rows = [row for row in rows if str(row["sid"]) in plan]
    if rows:
        result["status"] = "COMPLETE"
    if morning and rows:
        targets = {sid: item["report_date"] for sid, item in plan.items()}
        visible = {"data": rows}
        unavailable = collect_api_business_for_day(None, db, settings, visible, targets)
        result["api_business_status"] = unavailable["status"]
        business = collect_business_for_day(db, settings, visible, targets)
        result["business_status"] = business["status"]
        result["morning_dates"] = _business_dates(db, business, targets)
        result["failures"].extend(business.get("failures", []))
        if settings.lingxing_performance_enabled:
            health = collect_store_performance(client, db, stores_response=visible,
                settings=settings, health_only=True)
            result["performance_status"] = health["status"]
            for row in health["freshness"]:
                if row["performance_rows"] and row["performance_freshness"] not in {"MISSING", "FAILED", "STALE", "FUTURE"}:
                    sid = row["store_id"]
                    result["morning_dates"][sid] = targets[sid]
            result["failures"].extend(health.get("errors", []))
        result["excel"] = archive_partitions(db, settings, {
            sid: [(date.fromisoformat(day) - timedelta(days=1)).isoformat(), day]
            for sid, day in result["morning_dates"].items()
        })
    for row in rows:
        sid = str(row["sid"])
        item = plan[sid]
        if not item["intraday_enabled"] or int(row.get("has_ads_setting") or 0) != 1:
            continue
        try:
            captured = _utcnow()
            day = captured.astimezone(ZoneInfo(item["timezone"])).date().isoformat()
            result["observations"][sid] = run_observation(db, settings,
                {"store_ids": [sid], "observation_date": day}, client=client, now=captured)
        except Exception as exc:
            db.rollback()
            result["failures"].append({"step": "intraday", "store_id": sid, "error_type": type(exc).__name__})
    client.flush(db, raw_only=True)
    result["raw_archive"] = archive_raw_snapshots(db, settings.raw_dir, after_id=before)
    if result["failures"]:
        result["status"] = "PARTIAL_FAILED"
    return result
