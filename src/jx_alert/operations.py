"""Shared scheduled/manual jobs; business APIs remain read-only."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

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


def scoped_stores(response, scope_file):
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
    if {str(r.get("sid")) for r in selected} != requested:
        raise ValueError("A configured store was not returned by Lingxing")
    return {**response, "data": selected}


def collect_sp_targets(client, db, settings, report_dates, *, days=None):
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
        if str(sid) not in report_dates:
            raise ValueError("SP target is outside the configured store scope")
        if (sid, campaign) in seen:
            raise ValueError("SP targets must not contain duplicates")
        seen.add((sid, campaign))
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


def run_collection(*, report_date=None, backfill_days=1):
    """Run one locked pipeline and optional sales baseline backfill."""
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal, engine, init_db
    from jx_alert.storage.job_lock import database_job_lock
    from jx_alert.storage.store_config import build_store_report_dates, sync_store_config
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
        stores = scoped_stores(client.get_stores(), settings.collection_scope_file)
        latest = build_store_report_dates(stores, settings.store_timezones_file)
        if not latest:
            return {"status": "NO_ACTIVE_STORES"}
        latest = _completed_dates(report_date, latest)
        with SessionLocal() as db:
            # SP evidence commits independently; later collection failures cannot erase it.
            sp = collect_sp_targets(client, db, settings, latest,
                                    days=max(backfill_days, settings.sp_refresh_days))
            collected = collect_daily_snapshot(
                client, db, latest, stores_response=stores, settings=settings,
                ad_attribution_refresh_days=max(backfill_days, settings.ad_attribution_refresh_days))
            for offset in range(backfill_days - 1, 0, -1):
                dates = {sid: (date.fromisoformat(last) - timedelta(days=offset)).isoformat()
                         for sid, last in latest.items()}
                collect_sales_for_day(client, db, dates, stores_response=stores)
            sync_store_config(db, settings.store_timezones_file)
            db.commit()
            analysis = evaluate_daily_anomalies(db, latest)
            reports = [str(path) for sid, day in latest.items()
                       for path in export_daily_reports(db, day, settings.report_dir, store_id=sid)]
            from jx_alert.notifications.daily_report import enqueue_daily_snapshots
            notifications = enqueue_daily_snapshots(db, settings,
                                                     report_date=min(latest.values()),
                                                     store_dates=latest)
            db.commit()
            archive = archive_raw_snapshots(db, settings.raw_dir)
            return {"status": "PARTIAL" if sp["status"] == "PARTIAL" or
                    collected["status"] != "SUCCEEDED" else "COMPLETE",
                    "report_dates": latest, "collection": collected, "sp": sp,
                    "analysis": analysis, "reports": reports,
                    "daily_notifications": notifications, "raw_archive": archive}


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
        from jx_alert.storage.store_config import build_store_report_dates, load_store_config
        settings = get_settings()
        config = load_store_config(settings.store_timezones_file)
        stores = scoped_stores({"data": [{"sid": sid, "status": 1} for sid in config]},
                               settings.collection_scope_file)
        latest = build_store_report_dates(stores, settings.store_timezones_file)
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
        try:
            result = run_collection(report_date=args.get("report_date"),
                                    backfill_days=args.get("backfill_days", 1))
            if result["status"] == "COMPLETE":
                status = "SUCCEEDED"
                args.pop("retry_at", None)
                args.pop("error_type", None)
            elif result["status"] == "DRY_RUN":
                status = "SKIPPED"
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
