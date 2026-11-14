"""One complete daily business-data export, independent of provider collection."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
from jx_alert.storage.models import SyncJob


def run_daily_snapshot(*, snapshot_date=None, now=None):
    """Cron/recovery respects Beijing time; an explicit date is a manual export.

    Existing failed dates are retried without making new Lingxing API calls.
    The exporter publishes a manifest only after every workbook is complete.
    """
    from jx_alert.config import get_settings
    from jx_alert.reporting.daily_snapshot import export_daily_snapshot
    from jx_alert.storage.db import SessionLocal, engine
    settings = get_settings()
    if (not getattr(settings, "excel_archive_enabled", False)
            or not getattr(settings, "excel_daily_snapshot_enabled", True)):
        return {"status": "DISABLED"}
    if settings.app_dry_run:
        return {"status": "DRY_RUN"}
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Daily snapshots require an aware clock")
    local = clock.astimezone(ZoneInfo("Asia/Shanghai"))
    explicit = snapshot_date is not None
    day = date.fromisoformat(snapshot_date) if isinstance(snapshot_date, str) else snapshot_date
    if day is not None and (type(day) is not date or day > local.date()):
        raise ValueError("Snapshot date must be an existing Beijing calendar date")
    try:
        with database_job_lock(engine, "excel_daily_snapshot"), SessionLocal() as db:
            if not explicit:
                # A crash or disk failure before midnight must remain retryable.
                unfinished = db.scalars(select(SyncJob).where(
                    SyncJob.job_type == "excel_daily_snapshot", SyncJob.status.in_(["RUNNING", "FAILED"]),
                    SyncJob.started_at >= clock.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=7),
                ).order_by(SyncJob.id).limit(20)).all()
                retry_dates = []
                for row in unfinished:
                    try:
                        candidate = date.fromisoformat((row.metadata_json or {}).get("snapshot_date", ""))
                    except (TypeError, ValueError):
                        continue
                    if candidate <= local.date():
                        retry_dates.append(candidate)
                day = local.date()
                trigger = CronTrigger.from_crontab(
                    getattr(settings, "excel_daily_snapshot_cron", "30 23 * * *"), timezone="Asia/Shanghai")
                due = trigger.get_next_fire_time(None, datetime.combine(day, time.min, local.tzinfo))
                if due is None or due.date() != day:
                    raise ValueError("Snapshot cron has no delivery on this date")
                today_complete = db.scalar(select(SyncJob.id).where(
                    SyncJob.job_type == "excel_daily_snapshot", SyncJob.status == "SUCCEEDED",
                    SyncJob.metadata_json["snapshot_date"].as_string() == day.isoformat()).limit(1))
                # An older broken directory must not prevent today's archive.
                if local < due or today_complete is not None:
                    if retry_dates:
                        day = min(retry_dates)
                    elif local < due:
                        return {"status": "WAITING", "snapshot_date": day.isoformat(), "not_before": due.isoformat()}
            row = db.scalar(select(SyncJob).where(SyncJob.job_type == "excel_daily_snapshot",
                SyncJob.metadata_json["snapshot_date"].as_string() == day.isoformat())
                .order_by(SyncJob.id.desc()).limit(1))
            if row is None:
                row = SyncJob(job_type="excel_daily_snapshot", status="RUNNING",
                    started_at=clock.astimezone(timezone.utc).replace(tzinfo=None),
                    metadata_json={"snapshot_date": day.isoformat(), "attempts": 0})
                db.add(row)
            row.status, row.finished_at = "RUNNING", None
            row.metadata_json = {**(row.metadata_json or {}),
                                 "attempts": (row.metadata_json or {}).get("attempts", 0) + 1}
            db.commit()
            job_id = row.id
            try:
                result = export_daily_snapshot(db, settings.excel_archive_dir, day, now=clock, settings=settings)
                row = db.get(SyncJob, job_id)
                row.status, row.finished_at = "SUCCEEDED", datetime.now(timezone.utc).replace(tzinfo=None)
                row.record_count = result["row_count"]
                row.metadata_json = {**row.metadata_json, "manifest_path": result["manifest_path"],
                                     "file_count": len(result["files"]), "row_count": result["row_count"]}
                row.error_message = None
                db.commit()
                return {**result, "job_id": job_id}
            except Exception as exc:
                db.rollback()
                row = db.get(SyncJob, job_id)
                row.status, row.finished_at = "FAILED", datetime.now(timezone.utc).replace(tzinfo=None)
                row.error_message = type(exc).__name__
                db.commit()
                return {"status": "RETRY_PENDING", "snapshot_date": day.isoformat(),
                        "job_id": job_id, "error_type": type(exc).__name__}
    except JobAlreadyRunning:
        return {"status": "BUSY"}
