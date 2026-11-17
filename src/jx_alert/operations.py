"""Shared scheduled/manual jobs; business APIs remain read-only."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update

from jx_alert.store_controls import collection_enabled, collection_states, notification_states


class CollectionConfigurationError(ValueError):
    """A local configuration or collection scope must change before retrying."""


_COLLECTION_MAX_ATTEMPTS = 3
_COLLECTION_RETRY_DELAY_SECONDS = 60


def _collection_retry_at(failed_at, failures):
    delay = _COLLECTION_RETRY_DELAY_SECONDS * 2 ** max(failures - 1, 0)
    return (failed_at + timedelta(seconds=delay)).isoformat()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CollectionBlocked(CollectionConfigurationError):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _requested_store_ids(store_ids):
    if store_ids is None:
        return None
    if (not isinstance(store_ids, list) or not 1 <= len(store_ids) <= 100
            or any(not isinstance(sid, str) or not re.fullmatch(r"[1-9][0-9]{0,29}", sid) for sid in store_ids)):
        raise CollectionConfigurationError("store_ids must contain positive store ID strings")
    return sorted(set(store_ids))


def _checked_scope(settings, selected):
    path = Path(settings.collection_scope_file)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ids = raw["store_ids"]
        scope = {str(row["sid"]) for row in scoped_stores(
            {"data": [{"sid": str(int(sid))} for sid in ids]}, path)["data"]}
    except (KeyError, OSError, TypeError, ValueError):
        raise CollectionConfigurationError("Explicit store scope is not available") from None
    if selected is not None and not set(selected) <= scope:
        raise CollectionBlocked("STORE_OUT_OF_SCOPE")
    return scope


def _report_date(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise CollectionConfigurationError("report_date must use YYYY-MM-DD") from None
    return value


def _completed_dates(requested, latest):
    if requested is None:
        return latest
    if any(requested > last for last in latest.values()):
        raise CollectionConfigurationError("Only completed marketplace-local dates may be analyzed")
    return {sid: requested for sid in latest}


def scoped_stores(response, scope_file, *, allow_missing=False):
    """An optional explicit scope prevents collecting unrelated accounts."""
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Seller-list data must be an array")
    path = Path(scope_file)
    if not path.is_file():
        return response
    config = json.loads(path.read_text(encoding="utf-8"))
    ids = config.get("store_ids") if isinstance(config, dict) else None
    if (not isinstance(ids, list) or not ids
            or any(type(x) not in (str, int) or not str(x).isdigit() or int(x) <= 0
                   for x in ids)):
        raise ValueError("Collection scope requires positive store_ids")
    requested = {str(int(x)) for x in ids}
    selected = [r for r in rows if str(r.get("sid")) in requested]
    missing = requested - {str(r.get("sid")) for r in selected}
    if missing and not allow_missing:
        raise ValueError("A configured store was not returned by Lingxing")
    return {**response, "data": selected, **({"missing_store_ids": sorted(missing)} if missing else {})}


def store_report_plan(records, config_path, *, now=None, allow_unknown_timezone_history=False,
                      include_inactive=False):
    """Resolve each store separately without inventing its timezone or currency.

    The explicit opt-in history basis uses the completed date at UTC-12 as a
    mathematical lower bound for all current local dates. UTC-12 is never stored
    as a marketplace timezone and this basis cannot support intraday analysis.
    """
    try:
        root = json.loads(Path(config_path).read_text(encoding="utf-8"))
        config = root.get("stores") if isinstance(root, dict) else None
    except (OSError, ValueError) as exc:
        raise RuntimeError("Store configuration cannot be read or parsed") from exc
    if not isinstance(config, dict):
        raise RuntimeError("Store configuration must contain a stores object")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Store planning requires a timezone-aware clock")
    conservative = (clock.astimezone(timezone(timedelta(hours=-12))).date() - timedelta(days=1)).isoformat()
    plan = []
    for row in records:
        sid = str(row["sid"])
        raw = config.get(sid, {})
        if not isinstance(raw, dict):
            raw = {}
        tz = raw.get("timezone") or None
        currency = raw.get("currency_code") or ""
        currency = currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else ""
        item = {"store_id": sid, "enabled": True, "timezone": tz, "timezone_confirmed": False,
                "currency_code": currency, "currency_confirmed": bool(currency),
                "status_code": row.get("status"), "report_date": None, "date_basis": None,
                "state": "READY", "intraday_enabled": False}
        try:
            source_status = int(row.get("status") or 0)
        except (TypeError, ValueError):
            source_status = -1
        if not include_inactive and source_status != 1:
            item["state"] = "AUTHORIZATION_FAILED" if source_status == 2 else "INACTIVE"
        elif tz:
            try:
                local = clock.astimezone(ZoneInfo(tz))
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                item["state"] = "INVALID_TIMEZONE"
            else:
                item.update(report_date=(local.date() - timedelta(days=1)).isoformat(),
                            timezone_confirmed=True, date_basis="marketplace_local_complete", intraday_enabled=True)
        elif allow_unknown_timezone_history:
            item.update(report_date=conservative, date_basis="global_complete_unknown_timezone")
        else:
            item["state"] = "WAITING_TIMEZONE"
        plan.append(item)
    return plan


def _observed_sales_currency(db, sid):
    """Only complete sales observations may establish a store's sales currency."""
    from jx_alert.storage.models import DailyMetric, DataCoverage, Store
    currencies = set(db.scalars(select(DailyMetric.currency).join(DataCoverage,
        (DailyMetric.store_id == DataCoverage.store_id) &
        (DailyMetric.metric_date == DataCoverage.metric_date) &
        (DataCoverage.dimension == "store")).where(
            DailyMetric.store_id == sid, DailyMetric.dimension == "store", DailyMetric.metric_name == "sales",
            DailyMetric.source == "lingxing_sales_report", DataCoverage.status == "COMPLETE",
            DailyMetric.currency.is_not(None))).all())
    currencies = {x for x in currencies if isinstance(x, str) and re.fullmatch(r"[A-Z]{3}", x)}
    store = db.get(Store, sid)
    if len(currencies) == 1 and store and (not store.currency_code or store.currency_code in currencies):
        store.currency_code = next(iter(currencies))
        return "CONFIRMED_FROM_SALES"
    return "CURRENCY_CONFLICT" if len(currencies) > 1 or (currencies and store and store.currency_code not in currencies) else "WAITING_CURRENCY"


def sync_scoped_store_inventory(db, settings, stores_response, *, selected=None):
    """Save authorized metadata only; caller commits, no metrics or provider writes."""
    from jx_alert.lingxing.sales_collector import _upsert_store
    from jx_alert.storage.models import Store
    stores = scoped_stores(stores_response, settings.collection_scope_file, allow_missing=True)
    if selected is not None:
        stores = {**stores,
                  "data": [row for row in stores["data"] if str(row["sid"]) in selected],
                  "missing_store_ids": [sid for sid in stores.get("missing_store_ids", []) if sid in selected]}
    plan = store_report_plan(stores["data"], settings.store_timezones_file,
        allow_unknown_timezone_history=getattr(settings, "allow_unconfigured_timezone_history", False))
    for record in stores["data"]:
        _upsert_store(db, record)
    db.flush()
    for item in plan:
        store = db.get(Store, item["store_id"])
        if item["timezone_confirmed"]:
            store.timezone = item["timezone"]
        if item["currency_confirmed"]:
            store.currency_code = item["currency_code"]
    return {"stores_response": stores, "store_plan": plan}


def collect_sp_targets(client, db, settings, report_dates, *, days=None, scope_store_ids=None):
    if not settings.sp_collection_enabled:
        return {"status": "DISABLED", "runs": []}
    from jx_alert.lingxing.sp_validation import collect_sp_validation
    from jx_alert.lingxing.sp_persistence import persist_sp_validation
    config = json.loads(Path(settings.sp_validation_targets_file).read_text(encoding="utf-8"))
    targets = config.get("targets") if isinstance(config, dict) else None
    if not isinstance(targets, list) or not targets:
        raise ValueError("SP target configuration requires targets")
    count = settings.sp_refresh_days if days is None else days
    if type(count) is not int or not 1 <= count <= 30:
        raise ValueError("SP refresh days must be between 1 and 30")
    normalized, seen = [], set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("SP targets must be objects with positive IDs")
        raw = [target.get("sid"), target.get("campaign_id")]
        if any(type(x) not in (str, int) or not str(x).isdigit() or int(x) <= 0 for x in raw):
            raise ValueError("SP targets require positive integer IDs")
        sid, campaign = map(int, raw)
        if str(sid) not in (scope_store_ids if scope_store_ids is not None else report_dates):
            raise ValueError("SP target is outside the configured store scope")
        if (sid, campaign) in seen:
            raise ValueError("SP targets must not contain duplicates")
        seen.add((sid, campaign))
        if str(sid) not in report_dates:
            continue
        _report_date(report_dates[str(sid)])
        normalized.append((sid, campaign))
    runs = []
    for sid, campaign in normalized:
        if not collection_enabled(settings, sid):
            continue
        last = date.fromisoformat(report_dates[str(sid)])
        dates = [(last - timedelta(days=i)).isoformat()
                 for i in reversed(range(count))]
        result = collect_sp_validation(client, db, sid=sid, campaign_id=campaign,
                                       report_dates=dates)
        saved = persist_sp_validation(db, result)
        db.commit()
        if getattr(settings, "excel_archive_enabled", False):
            from jx_alert.reporting.archive import archive_sp_partition
            saved["excel_archive"] = archive_sp_partition(db, settings, saved["run_id"])
        runs.append(saved)
    return {"status": "PARTIAL" if any(r["status"] != "COMPLETE" for r in runs)
            else "COMPLETE", "runs": runs}


def run_collection(*, report_date=None, backfill_days=1, completed_store_ids=None,
                   completed_store_dates=None, progress=None, store_ids=None, planned_store_dates=None):
    """Run one locked pipeline and optional sales baseline backfill."""
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal, engine, init_db
    from jx_alert.storage.job_lock import database_job_lock
    from jx_alert.lingxing.source_gateway import create_collection_client
    from jx_alert.lingxing.pipeline import ad_account_inventory, collect_daily_snapshot
    from jx_alert.lingxing.sales_collector import collect_sales_for_day
    from jx_alert.analysis.evaluator import evaluate_daily_anomalies
    from jx_alert.reporting.excel import export_daily_reports
    from jx_alert.storage.raw_archive import archive_raw_snapshots
    if type(backfill_days) is not int or not 1 <= backfill_days <= 30:
        raise CollectionConfigurationError("backfill_days must be between 1 and 30")
    report_date = _report_date(report_date)
    selected = _requested_store_ids(store_ids)
    if planned_store_dates is not None:
        if (not isinstance(planned_store_dates, dict)
                or any(not isinstance(day, str) or _report_date(day) != day
                       for day in planned_store_dates.values())):
            raise CollectionConfigurationError("Invalid persisted report date plan")
    settings = get_settings()
    if settings.app_dry_run:
        return {"status": "DRY_RUN"}
    with database_job_lock(engine):
        allowed = None
        if selected is not None:
            allowed = _checked_scope(settings, selected)
            active = collection_states(settings, selected)
            if not any(active.values()):
                return {"status": "SKIPPED_COLLECTION_DISABLED", "collection_disabled_store_ids": selected}
        init_db()
        client = create_collection_client(settings)
        inventory = client.get_stores()
        with SessionLocal() as db:
            synced = sync_scoped_store_inventory(db, settings, inventory, selected=selected)
            db.commit()
        stores, plan = synced["stores_response"], synced["store_plan"]
        scope_ids = allowed if allowed is not None else {str(r["sid"]) for r in stores["data"]}
        missing = stores.get("missing_store_ids", [])
        switches = collection_states(settings, (x["store_id"] for x in plan))
        paused = sorted(sid for sid, enabled in switches.items() if not enabled)
        for item in plan:
            if item["store_id"] in paused:
                item.update(state="SKIPPED_COLLECTION_DISABLED", enabled=False)
        latest = {x["store_id"]: x["report_date"] for x in plan if x["state"] == "READY"}
        blocked = [x for x in plan if x["state"] != "READY"] + [
            {"store_id": sid, "enabled": True, "state": "SOURCE_STORE_MISSING"}
            for sid in missing]
        # Metadata is saved even for blocked stores, so the configured analysis
        # scope remains visible. No metric partition is fabricated for them.
        latest = _completed_dates(report_date, latest)
        if planned_store_dates is None:
            if progress is not None and latest:
                progress({"planned_store_dates": dict(latest)})
        else:
            # A retry remains on its original dates even after store midnight.
            # A changed timezone can make a previously planned day unsafe.
            pinned = {}
            for sid, due in latest.items():
                if sid not in planned_store_dates:
                    continue
                intended = planned_store_dates[sid]
                if intended > due:
                    item = next(row for row in plan if row["store_id"] == sid)
                    blocked.append({**item, "state": "PLANNED_DATE_NOT_COMPLETE"})
                    continue
                pinned[sid] = intended
            latest = pinned
        if not latest:
            return {"status": ("SKIPPED_COLLECTION_DISABLED" if blocked and
                    all(x["state"] == "SKIPPED_COLLECTION_DISABLED" for x in blocked)
                    else "CONFIGURATION_BLOCKED"), "store_plan": plan, "blocked_stores": blocked,
                    "completed_store_ids": []}
        # Store IDs alone do not prove which report date completed.
        completed = {str(sid) for sid, day in (completed_store_dates or {}).items()
                     if latest.get(str(sid)) == day}
        results, failures, sp_runs = {}, [], []
        accounts = ad_account_inventory(client)
        with SessionLocal() as db:
            from jx_alert.storage.models import RawSnapshot
            if accounts is not None:
                db.add(RawSnapshot(source="lingxing_seller_ad_accounts", payload={"rows": accounts}))
                db.commit()
            for sid, day in latest.items():
                if not collection_enabled(settings, sid):
                    results[sid] = {"status": "SKIPPED_COLLECTION_DISABLED"}
                    paused.append(sid)
                    completed.discard(sid)
                    continue
                if sid in completed:
                    results[sid] = {"status": "REUSED_COMPLETED_CHECKPOINT"}
                    continue
                one = {**stores, "data": [r for r in stores["data"] if str(r["sid"]) == sid]}
                try:
                    # Per-store checkpoints isolate provider failures and let a
                    # bounded manual retry retain already completed stores.
                    confirmed_timezone = next(p["timezone_confirmed"] for p in plan if p["store_id"] == sid)
                    sp = (collect_sp_targets(client, db, settings, {sid: day},
                        days=max(backfill_days, settings.sp_refresh_days), scope_store_ids=scope_ids)
                        if confirmed_timezone else {"status": "DISABLED_UNKNOWN_TIMEZONE", "runs": []})
                    sp_runs.extend(sp.get("runs", []))
                    for offset in range(backfill_days - 1, 0, -1):
                        if not collection_enabled(settings, sid):
                            break
                        older = (date.fromisoformat(day) - timedelta(days=offset)).isoformat()
                        collect_sales_for_day(client, db, {sid: older}, stores_response=one)
                    collected = collect_daily_snapshot(client, db, {sid: day}, stores_response=one,
                        settings=settings, ad_accounts=accounts,
                        ad_attribution_refresh_days=max(backfill_days, settings.ad_attribution_refresh_days))
                    if collected["status"] == "SKIPPED_COLLECTION_DISABLED":
                        results[sid] = collected
                        paused.append(sid)
                        continue
                    currency_state = _observed_sales_currency(db, sid)
                    db.commit()
                    analysis = evaluate_daily_anomalies(db, {sid: day})
                    reports = [str(p) for p in export_daily_reports(db, day, settings.report_dir, store_id=sid)]
                    status = ("PARTIAL" if sp["status"] == "PARTIAL" or
                        collected["status"] not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"} else
                        "COMPLETE_WITH_WARNINGS" if collected["status"] == "SUCCEEDED_WITH_WARNINGS" else "COMPLETE")
                    results[sid] = {"status": status, "collection": collected, "sp": sp,
                        "analysis": analysis, "reports": reports, "currency_state": currency_state}
                    if status in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
                        completed.add(sid)
                    else:
                        failures.append(sid)
                except Exception as exc:
                    db.rollback()
                    results[sid] = {"status": "FAILED", "error_type": type(exc).__name__}
                    failures.append(sid)
                if progress is not None:
                    progress({"completed_store_dates": {s: latest[s] for s in sorted(completed)},
                              "store_statuses": {s: r["status"] for s, r in results.items()}})
            archive = archive_raw_snapshots(db, settings.raw_dir)
        single = next(iter(results.values())) if len(results) == 1 else {}
        return {"status": "PARTIAL" if failures else "COMPLETE_WITH_WARNINGS" if blocked or paused or
                any(r["status"] == "COMPLETE_WITH_WARNINGS" for r in results.values()) else "COMPLETE",
                "report_dates": latest, "store_plan": plan, "blocked_stores": blocked, "stores": results,
                "collection_disabled_store_ids": sorted(set(paused)),
                "completed_store_ids": sorted(completed), "failed_store_ids": failures,
                "completed_store_dates": {sid: latest[sid] for sid in sorted(completed)},
                "collection": single.get("collection", {"by_store": results}),
                "sp": single.get("sp", {"status": "PARTIAL" if failures else "COMPLETE", "runs": sp_runs}),
                "analysis": single.get("analysis", {}), "reports": single.get("reports", []),
                "daily_notifications": {"queued": 0, "skipped": True, "reason": "scheduled_report"},
                "raw_archive": archive}


def queue_scheduled_daily_reports(*, now: datetime | None = None) -> dict:
    """Queue the 08:00 Beijing report from stored facts, independently of collection.

    Require explicit store scope and use the configured date policy for each store.
    No provider calls, collector lock, migrations or historical refresh are used.
    Missing/stale store mappings produce temporary unknown-data report records;
    they never create or update business Store rows.
    """
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal
    from jx_alert.storage.models import Store
    from jx_alert.notifications.daily_report import enqueue_daily_snapshots
    from apscheduler.triggers.cron import CronTrigger

    settings = get_settings()
    if not settings.feishu_daily_report_enabled or not settings.feishu_enabled:
        return {"status": "DISABLED", "queued": 0, "outbox_ids": []}
    if settings.app_dry_run:
        return {"status": "DRY_RUN", "queued": 0, "outbox_ids": []}
    observed = now if now is not None else datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Scheduled reports require a timezone-aware current time")
    delivery_now = observed.astimezone(ZoneInfo("Asia/Shanghai"))
    scheduled_day = delivery_now.date()
    start_date = getattr(settings, "feishu_daily_report_start_date", "")
    if start_date:
        start = date.fromisoformat(_report_date(start_date))
        scheduled_day = max(scheduled_day, start)
    trigger = CronTrigger.from_crontab(
        settings.feishu_daily_report_cron, timezone="Asia/Shanghai",
    )
    deadline = trigger.get_next_fire_time(None, datetime.combine(
        scheduled_day, datetime.min.time(), ZoneInfo("Asia/Shanghai"),
    ))
    if deadline is None or deadline.date() != scheduled_day:
        raise ValueError("Daily report cron has no delivery window on this date")
    if delivery_now < deadline:
        return {"status": "WAITING", "queued": 0, "outbox_ids": [],
                "delivery_date": scheduled_day.isoformat(),
                "not_before": deadline.isoformat()}

    scope_path = Path(settings.collection_scope_file)
    if not scope_path.is_file():
        raise ValueError("Scheduled daily reports require an explicit store scope file")
    raw_scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope_ids = raw_scope.get("store_ids", []) if isinstance(raw_scope, dict) else []
    selected = scoped_stores({"data": [{"sid": str(sid)} for sid in scope_ids]}, scope_path)["data"]
    selected_ids = sorted({str(row["sid"]) for row in selected})
    switches = notification_states(settings, selected_ids)
    paused = [sid for sid in selected_ids if not switches[sid][0]]
    selected_ids = [sid for sid in selected_ids if switches[sid][0]]
    if paused and not selected_ids:
        return {"status": "SKIPPED", "queued": 0, "outbox_ids": [],
                "reason": "store_notifications_disabled", "notification_disabled_store_ids": paused}
    with SessionLocal() as db:
        stores = {store.sid: store for store in db.scalars(
            select(Store).where(Store.sid.in_(selected_ids)),
        ).all()}
        plan = store_report_plan([{"sid": sid, "status": getattr(stores.get(sid), "status", 1)}
            for sid in selected_ids], settings.store_timezones_file, now=delivery_now,
            allow_unknown_timezone_history=getattr(settings, "allow_unconfigured_timezone_history", False),
            include_inactive=True)
        report_dates, store_records, warnings, history_fallback = {}, [], {}, set()
        for item in plan:
            if item["state"] != "READY":
                continue
            sid = item["store_id"]
            existing = stores.get(sid)
            if existing is None:
                store = Store(sid=sid, name="店铺" + sid, timezone=item["timezone"],
                              currency_code=item["currency_code"] or None)
                warnings[sid] = "尚未完成店铺数据同步"
            elif existing.timezone != item["timezone"]:
                store = Store(**{column.name: getattr(existing, column.name)
                                 for column in Store.__table__.columns})
                store.timezone = item["timezone"]
                if item["timezone_confirmed"]:
                    warnings[sid] = "时区配置已更新，等待重新采集校验"
            else:
                store = existing
            if existing is not None and existing.status != 1:
                note = "店铺当前非正常同步状态，数据待核实"
                warnings[sid] = warnings.get(sid, "") + ("；" if sid in warnings else "") + note
            if not item["timezone_confirmed"] and sid not in warnings:
                history_fallback.add(sid)
            report_dates[sid] = item["report_date"]
            store_records.append(store)
        if not report_dates:
            raise ValueError("No scoped database stores are available for scheduled reports")
        notifications = enqueue_daily_snapshots(
            db, settings, store_dates=report_dates, delivery_date=delivery_now.date(),
            store_records=store_records, store_warnings=warnings, now=delivery_now,
            history_fallback_store_ids=history_fallback,
            focused_morning=getattr(settings, "feishu_morning_focus_enabled", False),
        )
        db.commit()
    return {"status": "SKIPPED" if notifications.get("skipped") else "QUEUED",
            "delivery_date": delivery_now.date().isoformat(),
            "report_dates": report_dates, "store_plan": plan,
            "blocked_stores": [x for x in plan if x["state"] != "READY"],
            "notification_disabled_store_ids": paused, **notifications}


def enqueue_collection(db, *, report_date=None, backfill_days=1, idempotency_key=None, store_ids=None):
    """Queue bounded work so an MCP request can return immediately."""
    from jx_alert.storage.models import SyncJob
    if type(backfill_days) is not int or not 1 <= backfill_days <= 30:
        raise ValueError("backfill_days must be between 1 and 30")
    report_date = _report_date(report_date)
    selected = _requested_store_ids(store_ids)
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", idempotency_key)
    ):
        raise ValueError("idempotency_key must contain 1-100 letters, digits or _.:-")
    if report_date is not None or selected is not None:
        from jx_alert.config import get_settings
        from jx_alert.storage.models import Store
        settings = get_settings()
        configured = _checked_scope(settings, selected)
        scope = selected if selected is not None else sorted(configured)
        candidates = []
        for sid in scope:
            store = db.get(Store, sid)
            if selected is not None and store is None:
                raise CollectionBlocked("STORE_NOT_SYNCED")
            candidates.append({"sid": sid, "status": getattr(store, "status", 1)})
        plan = store_report_plan(candidates, settings.store_timezones_file,
            allow_unknown_timezone_history=getattr(settings, "allow_unconfigured_timezone_history", False))
        switches = collection_states(settings, (x["store_id"] for x in plan))
        if selected is not None:
            for item in plan:
                if not switches[item["store_id"]]:
                    raise CollectionBlocked("SKIPPED_COLLECTION_DISABLED")
                if item["state"] != "READY":
                    raise CollectionBlocked("STORE_UNAVAILABLE" if item["state"] == "INACTIVE" else item["state"])
        latest = {x["store_id"]: x["report_date"] for x in plan
                  if x["state"] == "READY" and switches[x["store_id"]]}
        if not latest and not any(not enabled for enabled in switches.values()):
            raise ValueError("No configured store dates are available")
        _completed_dates(report_date, latest)
    query = select(SyncJob).where(SyncJob.job_type == "manual_collection")

    def same_request(existing):
        args = existing.metadata_json or {}
        return (args.get("report_date") == report_date and args.get("backfill_days", 1) == backfill_days
                and args.get("store_ids") == selected)

    if idempotency_key is not None:
        existing = db.scalar(query.where(SyncJob.metadata_json["idempotency_key"].as_string() == idempotency_key)
                             .order_by(SyncJob.id.desc()).limit(1))
        if existing is not None:
            if not same_request(existing):
                raise ValueError("Idempotency key already belongs to different collection arguments")
            return {"job_id": existing.id, "status": existing.status, "reused": True}
    if idempotency_key is None:
        for existing in db.scalars(query.where(SyncJob.status.in_(["PENDING", "RUNNING"]))
                                   .order_by(SyncJob.id.desc())):
            if same_request(existing):
                return {"job_id": existing.id, "status": existing.status, "reused": True}
    metadata = {"report_date": report_date, "backfill_days": backfill_days,
                "attempts": 0, "idempotency_key": idempotency_key}
    if selected is not None:
        metadata["store_ids"] = selected
    job = SyncJob(job_type="manual_collection", status="PENDING",
                  metadata_json=metadata)
    db.add(job)
    db.flush()
    return {"job_id": job.id, "status": "PENDING", "reused": False}


def process_pending_collection():
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.models import SyncJob
    from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
    from jx_alert.store_controls import StoreControlError
    now = _utcnow()
    with database_job_lock(engine, "manual_dispatch"), SessionLocal() as db:
        # The dispatcher holds this crash-released lock for the whole collection.
        # Owning it proves any RUNNING dispatch is abandoned, without guessing an
        # expiry that might admit a second collector while the first is alive.
        abandoned = db.scalars(select(SyncJob).where(
            SyncJob.job_type.in_(["manual_collection", "intraday_observation"]), SyncJob.status == "RUNNING",
        )).all()
        for old in abandoned:
            args = dict(old.metadata_json or {})
            args["attempts"] = args.get("attempts", 0) + 1
            args["error_type"] = "InterruptedCollection"
            old.status = "PENDING" if args["attempts"] < _COLLECTION_MAX_ATTEMPTS else "FAILED"
            if old.status == "PENDING":
                args["retry_at"] = _collection_retry_at(now, args["attempts"])
            else:
                args.pop("retry_at", None)
            old.metadata_json = args
            old.finished_at = None if old.status == "PENDING" else now
        db.commit()
        job = db.scalar(select(SyncJob).where(SyncJob.job_type.in_(["manual_collection", "intraday_observation"]),
                                            SyncJob.status == "PENDING",
                                            or_(SyncJob.metadata_json["retry_at"].as_string().is_(None),
                                                SyncJob.metadata_json["retry_at"].as_string()
                                                <= now.isoformat()))
                        .order_by(SyncJob.id).limit(1))
        if job is None:
            return {"status": "IDLE"}
        args = dict(job.metadata_json or {})
        if "planned_store_dates" not in args:
            # Jobs written before date plans were persisted may already have
            # recorded the exact dates in their partial result.
            previous = args.get("result")
            previous_dates = previous.get("report_dates") if isinstance(previous, dict) else None
            if isinstance(previous_dates, dict) and previous_dates:
                args["planned_store_dates"] = previous_dates
        claim_token = uuid4().hex
        args["claim_token"] = claim_token
        args["claim_started_at"] = now.isoformat()
        claimed = db.execute(update(SyncJob).where(SyncJob.id == job.id,
                                       SyncJob.status == "PENDING").values(
                                           status="RUNNING", metadata_json=args,
                                           finished_at=None))
        if claimed.rowcount != 1:
            db.rollback()
            return {"job_id": job.id, "status": "LOST_CLAIM"}
        db.commit()
        def persist_progress(checkpoint):
            args.update(checkpoint)
            saved = db.execute(update(SyncJob).where(
                SyncJob.id == job.id, SyncJob.status == "RUNNING",
                SyncJob.metadata_json["claim_token"].as_string() == claim_token,
            ).values(metadata_json=dict(args)))
            if saved.rowcount != 1:
                db.rollback()
                raise RuntimeError("Collection claim was lost")
            db.commit()

        try:
            private_request = args.get("private_request")
            if private_request:
                from jx_alert.bot_service import _collection_permission, _question_delivery, validate_question_delivery
                from jx_alert.config import get_settings
                settings = get_settings()
                sid = (args.get("store_ids") or [None])[0]
                if (not _collection_permission(settings, private_request, sid)
                        or not validate_question_delivery(settings, _question_delivery(settings, private_request))):
                    raise CollectionBlocked("COLLECTION_PERMISSION_CHANGED")
            if job.job_type == "intraday_observation":
                from jx_alert.config import get_settings
                from jx_alert.lingxing.intraday_observation import run_observation
                result = run_observation(db, get_settings(), args)
            else:
                scope = {name: args[name] for name in ("store_ids", "planned_store_dates") if name in args}
                result = run_collection(
                    report_date=args.get("report_date"),
                    backfill_days=args.get("backfill_days", 1),
                    completed_store_ids=args.get("completed_store_ids"),
                    completed_store_dates=args.get("completed_store_dates"),
                    progress=persist_progress,
                    **scope,
                )
            if result.get("completed_store_ids"):
                args["completed_store_ids"] = result["completed_store_ids"]
                args["completed_store_dates"] = result.get("completed_store_dates", {})
            if result["status"] in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
                status = "SUCCEEDED_WITH_WARNINGS" if result["status"] == "COMPLETE_WITH_WARNINGS" else "SUCCEEDED"
                args.pop("error_type", None)
            elif result["status"] in {"DRY_RUN", "SKIPPED_COLLECTION_DISABLED"}:
                status = "SKIPPED"
            elif result["status"] == "CONFIGURATION_BLOCKED":
                status = "BLOCKED"
            else:
                args["attempts"] = args.get("attempts", 0) + 1
                args["error_type"] = "PartialCollection"
                status = "PENDING" if args["attempts"] < _COLLECTION_MAX_ATTEMPTS else "PARTIAL_FAILED"
            args["result"] = result
        except JobAlreadyRunning:
            status = "PENDING"
        except (CollectionConfigurationError, StoreControlError) as exc:
            status = "BLOCKED"
            args["error_type"] = type(exc).__name__
            args["result"] = {"status": "CONFIGURATION_BLOCKED"}
            if isinstance(exc, CollectionBlocked):
                args["result"]["reason"] = exc.status
        except Exception as exc:
            args["attempts"] = args.get("attempts", 0) + 1
            args["error_type"] = type(exc).__name__
            status = "PENDING" if args["attempts"] < _COLLECTION_MAX_ATTEMPTS else "FAILED"
        finished = _utcnow()
        if status == "PENDING":
            args["retry_at"] = _collection_retry_at(finished, args.get("attempts", 0))
        else:
            args.pop("retry_at", None)
        saved = db.execute(update(SyncJob).where(
            SyncJob.id == job.id, SyncJob.status == "RUNNING",
            SyncJob.metadata_json["claim_token"].as_string() == claim_token,
        ).values(status=status, metadata_json=args,
                 finished_at=None if status == "PENDING" else finished))
        if saved.rowcount != 1:
            db.rollback()
            return {"job_id": job.id, "status": "STALE_CLAIM"}
        db.commit()
        return {"job_id": job.id, "status": status}
