"""Read-only setup and operational checks. Never reveal IDs or credential contents."""
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.storage.models import Store, SyncJob


def _json_file(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


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
    return {"stores": stores, "manager_count": len(managers), "manual_mapping_required": missing,
            "scheduler_enabled": settings.scheduler_enabled, "cron": settings.daily_sync_cron,
            "cron_timezone": settings.app_timezone, "mcp_auth_mode": settings.mcp_auth_mode,
            "last_successful_collection": None if latest is None else {
                "job_id": latest.id, "status": latest.status, "finished_at_utc": str(latest.finished_at)},
            "health_collection_enabled": settings.lingxing_performance_enabled,
            "health_source_timezone_configured": bool(settings.lingxing_performance_source_timezone),
            "acceptance_note": "Configured flags do not prove external HTTPS or continuous-run acceptance"}
