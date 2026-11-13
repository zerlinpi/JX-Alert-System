"""Shared scheduled/manual jobs; business APIs remain read-only."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, update


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _report_date(value):
    if value is None:
        return None
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("report_date must use YYYY-MM-DD")
    return value


def _completed_dates(requested, latest):
    if requested is None:
        return latest
    if any(requested > last for last in latest.values()):
        raise ValueError("Only completed marketplace-local dates may be analyzed")
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


def sync_scoped_store_inventory(db, settings, stores_response):
    """Save authorized metadata only; caller commits, no metrics or provider writes."""
    from jx_alert.lingxing.sales_collector import _upsert_store
    from jx_alert.storage.models import Store
    stores = scoped_stores(stores_response, settings.collection_scope_file, allow_missing=True)
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
        last = date.fromisoformat(report_dates[str(sid)])
        dates = [(last - timedelta(days=i)).isoformat()
                 for i in reversed(range(count))]
        result = collect_sp_validation(client, db, sid=sid, campaign_id=campaign,
                                       report_dates=dates)
        saved = persist_sp_validation(db, result)
        db.commit()
        runs.append(saved)
    return {"status": "PARTIAL" if any(r["status"] != "COMPLETE" for r in runs)
            else "COMPLETE", "runs": runs}


def run_collection(*, report_date=None, backfill_days=1, completed_store_ids=None,
                   completed_store_dates=None, progress=None):
    """Run one locked pipeline and optional sales baseline backfill."""
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal, engine, init_db
    from jx_alert.storage.job_lock import database_job_lock
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.lingxing.pipeline import collect_daily_snapshot
    from jx_alert.lingxing.sales_collector import collect_sales_for_day
    from jx_alert.analysis.evaluator import evaluate_daily_anomalies
    from jx_alert.reporting.excel import export_daily_reports
    from jx_alert.storage.raw_archive import archive_raw_snapshots
    if type(backfill_days) is not int or not 1 <= backfill_days <= 30:
        raise ValueError("backfill_days must be between 1 and 30")
    report_date = _report_date(report_date)
    settings = get_settings()
    if settings.app_dry_run:
        return {"status": "DRY_RUN"}
    with database_job_lock(engine):
        init_db()
        client = LingxingClient.from_settings()
        inventory = client.get_stores()
        with SessionLocal() as db:
            synced = sync_scoped_store_inventory(db, settings, inventory)
            db.commit()
        stores, plan = synced["stores_response"], synced["store_plan"]
        latest = {x["store_id"]: x["report_date"] for x in plan if x["state"] == "READY"}
        blocked = [x for x in plan if x["state"] != "READY"] + [
            {"store_id": sid, "enabled": True, "state": "SOURCE_STORE_MISSING"}
            for sid in stores.get("missing_store_ids", [])]
        # Metadata is saved even for blocked stores, so the configured analysis
        # scope remains visible. No metric partition is fabricated for them.
        if not latest:
            return {"status": "CONFIGURATION_BLOCKED", "store_plan": plan, "blocked_stores": blocked,
                    "completed_store_ids": []}
        latest = _completed_dates(report_date, latest)
        scope_ids = {str(r["sid"]) for r in stores["data"]}
        # Store IDs alone do not prove which report date completed. A retry that
        # crosses a local midnight must not reuse yesterday's checkpoint today.
        completed = {str(sid) for sid, day in (completed_store_dates or {}).items()
                     if latest.get(str(sid)) == day}
        results, failures, sp_runs = {}, [], []
        with SessionLocal() as db:
            for sid, day in latest.items():
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
                        older = (date.fromisoformat(day) - timedelta(days=offset)).isoformat()
                        collect_sales_for_day(client, db, {sid: older}, stores_response=one)
                    collected = collect_daily_snapshot(client, db, {sid: day}, stores_response=one,
                        settings=settings, ad_attribution_refresh_days=max(backfill_days, settings.ad_attribution_refresh_days))
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
        return {"status": "PARTIAL" if failures else "COMPLETE_WITH_WARNINGS" if blocked or
                any(r["status"] == "COMPLETE_WITH_WARNINGS" for r in results.values()) else "COMPLETE",
                "report_dates": latest, "store_plan": plan, "blocked_stores": blocked, "stores": results,
                "completed_store_ids": sorted(completed), "failed_store_ids": failures,
                "completed_store_dates": {sid: latest[sid] for sid in sorted(completed)},
                "collection": single.get("collection", {"by_store": results}),
                "sp": single.get("sp", {"status": "PARTIAL" if failures else "COMPLETE", "runs": sp_runs}),
                "analysis": single.get("analysis", {}), "reports": single.get("reports", []),
                "daily_notifications": {"queued": 0, "skipped": True, "reason": "scheduled_report"},
                "raw_archive": archive}


def queue_scheduled_daily_reports(*, now: datetime | None = None) -> dict:
    """Queue the 08:00 Beijing report from stored facts, independently of collection.

    Explicit store scope and confirmed marketplace timezones are prerequisites.
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
    with SessionLocal() as db:
        stores = {store.sid: store for store in db.scalars(
            select(Store).where(Store.sid.in_(selected_ids)),
        ).all()}
        plan = store_report_plan([{"sid": sid, "status": getattr(stores.get(sid), "status", 1)}
            for sid in selected_ids], settings.store_timezones_file, now=delivery_now,
            allow_unknown_timezone_history=getattr(settings, "allow_unconfigured_timezone_history", False),
            include_inactive=True)
        report_dates, store_records, warnings = {}, [], {}
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
            report_dates[sid] = item["report_date"]
            store_records.append(store)
        if not report_dates:
            raise ValueError("No scoped database stores are available for scheduled reports")
        notifications = enqueue_daily_snapshots(
            db, settings, store_dates=report_dates, delivery_date=delivery_now.date(),
            store_records=store_records, store_warnings=warnings, now=delivery_now,
        )
        db.commit()
    return {"status": "SKIPPED" if notifications.get("skipped") else "QUEUED",
            "delivery_date": delivery_now.date().isoformat(),
            "report_dates": report_dates, "store_plan": plan,
            "blocked_stores": [x for x in plan if x["state"] != "READY"], **notifications}


def enqueue_collection(db, *, report_date=None, backfill_days=1, idempotency_key=None):
    """Queue bounded work so an MCP request can return immediately."""
    from jx_alert.storage.models import SyncJob
    if type(backfill_days) is not int or not 1 <= backfill_days <= 30:
        raise ValueError("backfill_days must be between 1 and 30")
    report_date = _report_date(report_date)
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", idempotency_key)
    ):
        raise ValueError("idempotency_key must contain 1-100 letters, digits or _.:-")
    if report_date is not None:
        from jx_alert.config import get_settings
        from jx_alert.storage.models import Store
        settings = get_settings()
        raw = json.loads(Path(settings.collection_scope_file).read_text(encoding="utf-8"))
        candidates = [{"sid": str(sid), "status": getattr(db.get(Store, str(sid)), "status", 1)}
                      for sid in raw.get("store_ids", [])]
        stores = scoped_stores({"data": candidates}, settings.collection_scope_file)
        plan = store_report_plan(stores["data"], settings.store_timezones_file,
            allow_unknown_timezone_history=getattr(settings, "allow_unconfigured_timezone_history", False))
        latest = {x["store_id"]: x["report_date"] for x in plan if x["state"] == "READY"}
        if not latest:
            raise ValueError("No configured store dates are available")
        _completed_dates(report_date, latest)
    query = select(SyncJob).where(SyncJob.job_type == "manual_collection")
    if idempotency_key is not None:
        query = query.where(SyncJob.metadata_json["idempotency_key"].as_string() == idempotency_key)
    else:
        query = query.where(SyncJob.status.in_(["PENDING", "RUNNING"]))
    for existing in db.scalars(query.order_by(SyncJob.id.desc())).all():
        args = existing.metadata_json or {}
        same = args.get("report_date") == report_date and args.get("backfill_days", 1) == backfill_days
        if idempotency_key is not None and not same:
            raise ValueError("Idempotency key already belongs to different collection arguments")
        if same:
            return {"job_id": existing.id, "status": existing.status, "reused": True}
    job = SyncJob(job_type="manual_collection", status="PENDING",
                  metadata_json={"report_date": report_date, "backfill_days": backfill_days,
                                 "attempts": 0, "idempotency_key": idempotency_key})
    db.add(job)
    db.flush()
    return {"job_id": job.id, "status": "PENDING", "reused": False}


def process_pending_collection():
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.models import SyncJob
    from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
    now = _utcnow()
    with database_job_lock(engine, "manual_dispatch"), SessionLocal() as db:
        # The dispatcher holds this crash-released lock for the whole collection.
        # Owning it proves any RUNNING dispatch is abandoned, without guessing an
        # expiry that might admit a second collector while the first is alive.
        abandoned = db.scalars(select(SyncJob).where(
            SyncJob.job_type == "manual_collection", SyncJob.status == "RUNNING",
        )).all()
        for old in abandoned:
            args = dict(old.metadata_json or {})
            args["attempts"] = args.get("attempts", 0) + 1
            args["error_type"] = "InterruptedCollection"
            args["retry_at"] = now.isoformat()
            old.status = "PENDING" if args["attempts"] < 3 else "FAILED"
            old.metadata_json = args
            old.finished_at = None if old.status == "PENDING" else now
        db.commit()
        job = db.scalar(select(SyncJob).where(SyncJob.job_type == "manual_collection",
                                            SyncJob.status == "PENDING",
                                            or_(SyncJob.metadata_json["retry_at"].as_string().is_(None),
                                                SyncJob.metadata_json["retry_at"].as_string()
                                                <= now.isoformat()))
                        .order_by(SyncJob.id).limit(1))
        if job is None:
            return {"status": "IDLE"}
        args = dict(job.metadata_json or {})
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
        retry = False
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
            result = run_collection(report_date=args.get("report_date"),
                                    backfill_days=args.get("backfill_days", 1),
                                    completed_store_ids=args.get("completed_store_ids"),
                                    completed_store_dates=args.get("completed_store_dates"),
                                    progress=persist_progress)
            if result.get("completed_store_ids"):
                args["completed_store_ids"] = result["completed_store_ids"]
                args["completed_store_dates"] = result.get("completed_store_dates", {})
            if result["status"] in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
                status = "SUCCEEDED_WITH_WARNINGS" if result["status"] == "COMPLETE_WITH_WARNINGS" else "SUCCEEDED"
                args.pop("retry_at", None)
                args.pop("error_type", None)
            elif result["status"] == "DRY_RUN":
                status = "SKIPPED"
            elif result["status"] == "CONFIGURATION_BLOCKED":
                status = "BLOCKED"
            else:
                args["attempts"] = args.get("attempts", 0) + 1
                args["error_type"] = "PartialCollection"
                retry = True
                status = "PENDING" if args["attempts"] < 3 else "PARTIAL_FAILED"
            args["result"] = result
        except JobAlreadyRunning:
            status = "PENDING"
            retry = True
        except Exception as exc:
            args["attempts"] = args.get("attempts", 0) + 1
            args["error_type"] = type(exc).__name__
            retry = True
            status = "PENDING" if args["attempts"] < 3 else "FAILED"
        finished = _utcnow()
        if retry and status == "PENDING":
            delay = 30 * 2 ** max(args.get("attempts", 0) - 1, 0)
            args["retry_at"] = (finished + timedelta(seconds=delay)).isoformat()
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
