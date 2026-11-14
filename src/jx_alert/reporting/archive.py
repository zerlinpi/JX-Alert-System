"""Archive committed partitions without turning a disk failure into an API retry."""
from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import select

from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
from jx_alert.storage.models import SyncJob


def archive_partitions(db, settings, report_dates):
    if not getattr(settings, "excel_archive_enabled", False):
        return {"status": "DISABLED", "files": []}
    from jx_alert.reporting.excel import archive_collected_store
    paths, errors = [], []
    for sid, dates in report_dates.items():
        for day in [dates] if isinstance(dates, str) else dates:
            identity = f"{sid}:{day}"
            lock = "excel_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            try:
                with database_job_lock(db.get_bind(), lock):
                    outputs = archive_collected_store(db, sid, day, settings.excel_archive_dir, settings=settings)
                    paths.extend(str(p) for p in outputs)
                    pending = db.scalars(select(SyncJob).where(SyncJob.job_type == "excel_archive",
                        SyncJob.status == "PENDING", SyncJob.metadata_json["partition"].as_string() == identity)).all()
                    for row in pending:
                        row.status, row.finished_at = "SUCCEEDED", datetime.now(timezone.utc).replace(tzinfo=None)
                    db.commit()
            except Exception as exc:
                db.rollback()
                errors.append({"store_id": sid, "report_date": day, "error_type": type(exc).__name__})
                pending = db.scalar(select(SyncJob).where(SyncJob.job_type == "excel_archive", SyncJob.status == "PENDING",
                    SyncJob.metadata_json["partition"].as_string() == identity))
                if pending is None:
                    db.add(SyncJob(job_type="excel_archive", status="PENDING", started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        metadata_json={"partition": identity, "store_id": sid, "report_date": day, "attempts": 0}))
                    db.commit()
    return {"status": "RETRY_PENDING" if errors else "COMPLETE", "files": paths, "errors": errors}


def retry_pending_archives():
    from jx_alert.config import get_settings
    from jx_alert.reporting.excel import archive_collected_store, archive_sp_run
    from jx_alert.storage.db import SessionLocal, engine
    settings = get_settings()
    if not settings.excel_archive_enabled:
        return {"status": "DISABLED"}
    clock = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with database_job_lock(engine, "excel_archive_retry"), SessionLocal() as db:
            jobs = db.scalars(select(SyncJob).where(SyncJob.job_type == "excel_archive", SyncJob.status == "PENDING")
                              .order_by(SyncJob.id).limit(5)).all()
            for row in jobs:
                meta = dict(row.metadata_json)
                if meta.get("retry_at") and datetime.fromisoformat(meta["retry_at"]) > clock:
                    continue
                lock = "excel_" + hashlib.sha256(meta["partition"].encode()).hexdigest()[:24]
                try:
                    with database_job_lock(engine, lock):
                        if meta.get("kind") == "sp":
                            archive_sp_run(db, meta["run_id"], settings.excel_archive_dir)
                        else:
                            archive_collected_store(db, meta["store_id"], meta["report_date"], settings.excel_archive_dir, settings=settings)
                    row.status, row.finished_at = "SUCCEEDED", clock
                except JobAlreadyRunning:
                    continue
                except Exception as exc:
                    meta["attempts"] = meta.get("attempts", 0) + 1
                    meta["error_type"] = type(exc).__name__
                    meta["retry_at"] = (clock + timedelta(minutes=2 ** min(meta["attempts"], 5))).isoformat()
                    row.metadata_json = meta
                    if meta["attempts"] >= 5:
                        row.status, row.finished_at = "FAILED", clock
                db.commit()
            return {"status": "CHECKED", "jobs": len(jobs)}
    except JobAlreadyRunning:
        return {"status": "BUSY"}


def archive_sp_partition(db, settings, run_id):
    from jx_alert.reporting.excel import archive_sp_run
    try:
        return {"status": "COMPLETE", "files": [str(p) for p in archive_sp_run(db, run_id, settings.excel_archive_dir)]}
    except Exception as exc:
        identity = "sp:" + run_id
        pending = db.scalar(select(SyncJob).where(SyncJob.job_type == "excel_archive", SyncJob.status == "PENDING",
            SyncJob.metadata_json["partition"].as_string() == identity))
        if pending is None:
            db.add(SyncJob(job_type="excel_archive", status="PENDING", started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                metadata_json={"partition": identity, "kind": "sp", "run_id": run_id, "attempts": 0}))
            db.commit()
        return {"status": "RETRY_PENDING", "error_type": type(exc).__name__}
