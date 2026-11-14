import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jx_alert.config import get_settings
from jx_alert.storage.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("jx_alert.scheduler")


def daily_job(*, now=None):
    """Persist the daily plan; the existing dispatcher owns collection retries."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from jx_alert.operations import enqueue_collection
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning

    try:
        if get_settings().app_dry_run:
            logger.info("Daily collection status=DRY_RUN")
            return
        clock = now if now is not None else datetime.now(timezone.utc)
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("Scheduled collection requires a timezone-aware clock")
        day = clock.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        # A fixed Beijing delivery day identifies the plan, while each store's
        # completed local report date is still resolved by the dispatcher. Two
        # sales dates cover a missed previous collection without inventing facts.
        with database_job_lock(engine, "scheduled_collection"), SessionLocal() as db:
            result = enqueue_collection(db, report_date=None, backfill_days=2,
                                        idempotency_key="scheduled-collection:" + day)
            db.commit()
        logger.info("Daily collection queued job=%s status=%s reused=%s",
                    result["job_id"], result["status"], result["reused"])
    except JobAlreadyRunning:
        logger.info("Daily collection enqueue is already running")
    except Exception as exc:
        logger.error("Daily collection enqueue failed: %s", type(exc).__name__)


def manual_collection_job():
    from jx_alert.operations import process_pending_collection
    from jx_alert.storage.job_lock import JobAlreadyRunning
    try:
        process_pending_collection()
    except JobAlreadyRunning:
        return
    except Exception as exc:
        logger.error("Manual collection failed: %s", type(exc).__name__)


def outbox_job():
    from jx_alert.notifications.worker import process_outbox_batch
    result = process_outbox_batch(batch_size=5)
    if result.get("claimed"):
        logger.info("Feishu outbox batch: %s", result)


def excel_archive_job():
    from jx_alert.reporting.archive import retry_pending_archives
    try:
        retry_pending_archives()
    except Exception as exc:
        logger.error("Excel archive retry failed: %s", type(exc).__name__)


def daily_excel_snapshot_job():
    from jx_alert.reporting.daily_job import run_daily_snapshot
    try:
        result = run_daily_snapshot()
        if result["status"] not in {"DISABLED", "WAITING", "ALREADY_COMPLETE", "BUSY"}:
            logger.info("Daily Excel snapshot status=%s", result["status"])
    except Exception as exc:
        logger.error("Daily Excel snapshot failed: %s", type(exc).__name__)


def daily_report_job():
    """Send only the independently queued Beijing daily reports, without backlog."""
    from jx_alert.operations import queue_scheduled_daily_reports
    from jx_alert.notifications.worker import process_outbox_batch
    try:
        result = queue_scheduled_daily_reports()
        ids = list(dict.fromkeys(result.get("outbox_ids") or []))
        if any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError("Scheduled report IDs must be positive integers")
        sent = claimed = 0
        # The worker deliberately caps one claim to five messages. Scope-bound
        # report IDs are split into finite batches rather than draining backlog.
        for offset in range(0, len(ids), 5):
            batch = ids[offset:offset + 5]
            delivery = process_outbox_batch(batch_size=len(batch), outbox_ids=batch)
            sent += delivery.get("sent", 0)
            claimed += delivery.get("claimed", 0)
        logger.info("Scheduled daily reports status=%s queued=%s claimed=%s sent=%s",
                    result["status"], len(ids), claimed, sent)
    except Exception as exc:
        logger.error("Scheduled daily reports failed: %s", type(exc).__name__)


def main():
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false; scheduler process exits without starting jobs.")
        return

    init_db()
    scheduler = BlockingScheduler(
        timezone=settings.app_timezone,
        executors={"default": ThreadPoolExecutor(max_workers=4),
                   "daily_reports": ThreadPoolExecutor(max_workers=1)},
    )
    scheduler.add_job(
        daily_report_job,
        CronTrigger.from_crontab(settings.feishu_daily_report_cron, timezone="Asia/Shanghai"),
        id="feishu_daily_report", executor="daily_reports",
        replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        daily_report_job, IntervalTrigger(minutes=1),
        id="feishu_daily_report_recovery", executor="daily_reports",
        replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        daily_job,
        CronTrigger.from_crontab(settings.daily_sync_cron, timezone="Asia/Shanghai"),
        id="daily_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        outbox_job,
        IntervalTrigger(seconds=10),
        id="feishu_outbox",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        manual_collection_job, IntervalTrigger(seconds=30),
        id="manual_collection", max_instances=1, coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(excel_archive_job, IntervalTrigger(minutes=1), id="excel_archive_retry",
                      max_instances=1, coalesce=True, misfire_grace_time=60)
    if getattr(settings, "excel_archive_enabled", False) and getattr(settings, "excel_daily_snapshot_enabled", True):
        scheduler.add_job(daily_excel_snapshot_job,
            CronTrigger.from_crontab(getattr(settings, "excel_daily_snapshot_cron", "30 23 * * *"),
                                    timezone="Asia/Shanghai"),
            id="excel_daily_snapshot", max_instances=1, coalesce=True, misfire_grace_time=1800)
        scheduler.add_job(daily_excel_snapshot_job, IntervalTrigger(minutes=5),
            id="excel_daily_snapshot_recovery", max_instances=1, coalesce=True, misfire_grace_time=60)
    logger.info(
        "Scheduler started (daily_timezone=Asia/Shanghai, collection_cron=%s, report_cron=%s, dry_run=%s)",
        settings.daily_sync_cron, settings.feishu_daily_report_cron, settings.app_dry_run,
    )
    # The independent gate prevents pre-08:00 sends and respects the configured
    # first delivery date; persistent idempotency makes restarts safe.
    daily_report_job()
    daily_excel_snapshot_job()
    scheduler.start()


if __name__ == "__main__":
    main()
