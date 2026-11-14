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


def group_bot_status(settings):
    """Read activation flags without resetting MCP's cached OAuth configuration."""
    runtime = _json_file(getattr(settings, "runtime_settings_file", ""))
    def value(key, fallback):
        configured = getattr(settings, key, fallback)
        latest = runtime.get(key, configured)
        return latest if type(latest) is type(fallback) else configured
    key_file = value("deepseek_credentials_file", "/app/data/secrets/deepseek.json")
    return {
        "feishu_bot_enabled": value("feishu_bot_enabled", False),
        "deepseek_credentials_present": bool(getattr(settings, "deepseek_api_key", "")) or Path(key_file).is_file(),
        "deepseek_model": value("deepseek_model", "deepseek-flash"),
        "excel_archive_enabled": value("excel_archive_enabled", False),
        "excel_archive_directory": value("excel_archive_dir", "/app/data/excel"),
        "bot_status_check_scope": "configuration_only",
    }


def feishu_channel_status(settings):
    """Inspect only the selected transport locally; never request tokens or send messages."""
    transport = getattr(settings, "feishu_transport", "webhook")
    app_configured = None
    configured = False
    status = "NOT_CONFIGURED"
    if transport == "app":
        from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError
        try:
            FeishuAppClient.from_settings(settings)
        except (FeishuAppError, ValueError, TypeError, AttributeError):
            # Never expose private paths, values or the original exception text.
            app_configured = False
            status = "INVALID_CONFIGURATION"
        else:
            app_configured = configured = True
            status = "CONFIGURED"
        name = "飞书自建应用"
        note = ("当前使用飞书自建应用，" + ("应用凭据已配置" if configured else "应用凭据需核实")
                + "；此模式不需要Webhook。实际送达以通知日志为准。")
    elif transport == "webhook":
        configured = bool(getattr(settings, "feishu_webhook_url", ""))
        status = "CONFIGURED" if configured else "NOT_CONFIGURED"
        name = "飞书Webhook"
        note = "仅检查默认Webhook配置；按群路由需另查对应配置。实际送达以通知日志为准。"
    else:
        transport, name, status = "unknown", "未知通道", "INVALID_TRANSPORT"
        note = "通知通道配置无效，需要核实；未回退到其他通道。"
    report_group = None
    if transport == "app" and getattr(settings, "feishu_routing_file", None):
        from jx_alert.notifications.routing import ApplicationRoutingError, report_group_delivery
        routing = _json_file(settings.feishu_routing_file)
        try:
            report_group_delivery(settings, routing)
        except ApplicationRoutingError:
            pass
        else:
            alias = settings.feishu_daily_report_chat_id
            report_group = routing["groups"][alias].get("display_name") or alias
    return {
        "feishu_transport": transport, "feishu_channel_name": name,
        "feishu_transport_configured": configured,
        "feishu_app_credentials_configured": app_configured,
        "feishu_webhook_required": transport == "webhook",
        "feishu_configuration_status": status,
        "feishu_report_group": report_group,
        "feishu_delivery_enabled": bool(configured and getattr(settings, "feishu_enabled", False)
                                        and not getattr(settings, "app_dry_run", True)),
        "feishu_check_scope": "configuration_only", "feishu_status_note": note,
    }


def analysis_plan(db, settings, *, now=None):
    """Enumerate the complete enabled scope and distinguish blocked/missing data."""
    from jx_alert.operations import scoped_stores, store_report_plan
    from jx_alert.chat_analysis import snapshot_comparisons, comparison_summary, METRICS
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
            rows = db.scalars(select(DailyMetric).where(DailyMetric.store_id == store.sid,
                DailyMetric.dimension == "store", DailyMetric.entity_id == store.sid,
                DailyMetric.metric_date.in_(dates), DailyMetric.metric_name.in_(METRICS))).all()
            facts = [{"entity_id": r.entity_id, "metric": r.metric_name, "date": r.metric_date.isoformat(),
                      "value": r.value, "currency": r.currency, "complete": r.metric_date in coverage} for r in rows]
            comparisons = snapshot_comparisons(facts, last, "store", confirmed_currency=store.currency_code,
                                               entity_ids=[store.sid])
            comparison = comparison_summary(comparisons)
            item.update(comparison)
            item["missing_baseline_dates"] = [d.isoformat() for d in dates if d not in coverage]
            item["state"] = ("NO_DATA" if not any(r.metric_date == last for r in rows) else
                "READY" if comparison["comparison_coverage"] == "COMPLETE" else
                "READY_WITH_GAPS" if comparison["data_sufficient"] else "INSUFFICIENT_DATA")
            item["analysis_ready"] = comparison["data_sufficient"]
            item["amount_analysis_ready"] = any(r["metric"] in {"sales", "spend"}
                for r in comparison["eligible_comparisons"])
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
    report.update(feishu_channel_status(settings))
    fallback_enabled = bool(getattr(settings, "feishu_group_fallback_enabled", False))
    report["feishu_group_fallback_enabled"] = fallback_enabled
    if fallback_enabled:
        from jx_alert.notifications.routing import ApplicationRoutingError, report_group_delivery
        try:
            report_group_delivery(settings, routing)
        except ApplicationRoutingError:
            report["feishu_group_fallback_configured"] = False
        else:
            report["feishu_group_fallback_configured"] = True
            optional_roles = {"admin_count", "operator_count", "management.open_ids"}
            report["optional_mention_mappings"] = [item for item in missing if item["field"] in optional_roles]
            report["manual_mapping_required"] = [item for item in missing if item["field"] not in optional_roles]
            report["feishu_group_fallback_note"] = "人员未映射时使用已配置日报群。实际送达以通知日志为准。"
    return report
