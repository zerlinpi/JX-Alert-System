"""Read-only setup and operational checks. Never reveal IDs or credential contents."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.storage.models import DailyMetric, DataCoverage, Store, SyncJob


def _json_file(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def analysis_plan(db, settings, *, now=None):
    """Enumerate the complete enabled scope and distinguish blocked/missing data."""
    from jx_alert.operations import scoped_stores, store_report_plan
    raw = _json_file(settings.collection_scope_file)
    ids = raw.get("store_ids", [])
    if not isinstance(ids, list):
        raise ValueError("Collection scope requires a store_ids array")
    scoped = scoped_stores({"data": [{"sid": str(sid)} for sid in ids]}, settings.collection_scope_file)["data"]
    existing = {str(row["sid"]): db.get(Store, str(row["sid"])) for row in scoped}
    records = [{"sid": sid, "status": getattr(store, "status", 1)} for sid, store in existing.items()]
    plan = store_report_plan(records, settings.store_timezones_file, now=now,
        allow_unknown_timezone_history=getattr(settings, "allow_unconfigured_timezone_history", False))
    for item in plan:
        store = existing[item["store_id"]]
        item["store_name"] = store.name if store else None
        item["collection_ready"] = item["state"] == "READY"
        item["currency_code"] = getattr(store, "currency_code", None) or item["currency_code"] or None
        item["currency_confirmed"] = bool(item["currency_code"])
        item["analysis_ready"] = False
        item["amount_analysis_ready"] = False
        if store is None:
            item["state"] = "WAITING_STORE_SYNC"
        elif item["state"] == "READY":
            last = datetime.fromisoformat(item["report_date"]).date()
            dates = [last - timedelta(days=i) for i in range(8)]
            coverage = {r.metric_date for r in db.scalars(select(DataCoverage).where(
                DataCoverage.store_id == store.sid, DataCoverage.dimension == "store",
                DataCoverage.metric_date.in_(dates), DataCoverage.status == "COMPLETE"))}
            fact = db.scalar(select(DailyMetric.id).where(DailyMetric.store_id == store.sid,
                DailyMetric.dimension == "store", DailyMetric.metric_date == last).limit(1))
            item["missing_baseline_dates"] = [d.isoformat() for d in dates if d not in coverage]
            item["state"] = "NO_DATA" if fact is None else "INSUFFICIENT_DATA" if len(coverage) < 8 else "READY"
            item["analysis_ready"] = item["state"] == "READY"
            item["amount_analysis_ready"] = item["analysis_ready"] and item["currency_confirmed"]
    return {"checked_at_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "enabled_store_count": len(plan), "collection_ready_count": sum(x["collection_ready"] for x in plan),
            "analysis_ready_count": sum(x["analysis_ready"] for x in plan), "stores": plan}


def configuration_status(db, settings):
    routing = _json_file(settings.feishu_routing_file)
    scope = _json_file(settings.collection_scope_file).get("store_ids", [])
    if not isinstance(scope, list):
        scope = []
    stores, missing = [], []
    for sid in map(str, scope):
        store = db.get(Store, sid)
        def entry_at(section, key):
            mapping = routing.get(section, {})
            value = mapping.get(key, {}) if isinstance(mapping, dict) else {}
            return value if isinstance(value, dict) else {}

        def ids(value):
            return value if isinstance(value, list) and all(
                isinstance(uid, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid) for uid in value
            ) else []

        entry = entry_at("stores", sid)
        alias = entry.get("chat_id", routing.get("default_chat_id", ""))
        group = entry_at("groups", alias) if isinstance(alias, str) else {}
        timezone = getattr(store, "timezone", None)
        valid_timezone = False
        try:
            valid_timezone = bool(timezone) and bool(ZoneInfo(timezone))
        except (ValueError, ZoneInfoNotFoundError):
            pass
        item = {"store_id": sid, "timezone": timezone, "timezone_valid": valid_timezone,
                "currency": getattr(store, "currency_code", None),
                "group_configured": bool(re.fullmatch(r"oc_[A-Za-z0-9_-]+", str(group.get("chat_id", "")))),
                "admin_count": len(ids(group.get("admin_open_ids", []))),
                "operator_count": len(ids(entry.get("open_ids", [])))}
        stores.append(item)
        if not valid_timezone:
            missing.append({"store_id": sid, "field": "verified_marketplace_timezone"})
        for field in ("group_configured", "admin_count", "operator_count"):
            if not item[field]:
                missing.append({"store_id": sid, "field": field})
    management = routing.get("management", {})
    managers = management.get("open_ids", []) if isinstance(management, dict) else []
    if not isinstance(managers, list) or any(
        not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid) for uid in managers
    ):
        managers = []
    if not managers:
        missing.append({"field": "management.open_ids"})
    if not settings.lingxing_performance_source_timezone:
        missing.append({"field": "verified_health_source_timezone"})
    if not settings.mcp_public_url:
        missing.append({"field": "mcp_public_url_dns_tls"})
    latest = db.scalar(select(SyncJob).where(SyncJob.job_type == "daily_collection",
                                            SyncJob.status.in_(["SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"]))
                       .order_by(SyncJob.id.desc()).limit(1))
    report = {"stores": stores, "manager_count": len(managers), "manual_mapping_required": missing,
            "scheduler_enabled": settings.scheduler_enabled, "cron": settings.daily_sync_cron,
            "cron_timezone": "Asia/Shanghai", "mcp_auth_mode": settings.mcp_auth_mode,
            "daily_report_cron": getattr(settings, "feishu_daily_report_cron", "0 8 * * *"),
            "daily_report_timezone": "Asia/Shanghai",
            "daily_report_start_date": getattr(settings, "feishu_daily_report_start_date", ""),
            "business_alert_delivery_mode": getattr(settings, "business_alert_delivery_mode", "rules"),
            "chat_notifications_enabled": getattr(settings, "mcp_notifications_enabled", False),
            "last_successful_collection": None if latest is None else {
                "job_id": latest.id, "status": latest.status, "finished_at_utc": str(latest.finished_at)},
            "health_collection_enabled": settings.lingxing_performance_enabled,
            "health_observation_only": getattr(settings, "lingxing_performance_observation_only", True),
            "health_source_timezone_configured": bool(settings.lingxing_performance_source_timezone),
            "acceptance_note": "Configured flags do not prove external HTTPS or continuous-run acceptance"}
    if hasattr(settings, "store_timezones_file"):
        try:
            report["analysis_plan"] = analysis_plan(db, settings)
        except (OSError, ValueError, RuntimeError):
            report["analysis_plan"] = {"status": "CONFIGURATION_INVALID", "enabled_store_count": len(scope)}
    return report
