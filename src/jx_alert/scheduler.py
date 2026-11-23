import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jx_alert.config import effective_daily_sync_cron, get_settings
from jx_alert.reporting.company_executive import BEIJING as COMPANY_BEIJING, CLOSES as COMPANY_CLOSES
from jx_alert.reporting.company_executive import OPENS as COMPANY_OPENS
from jx_alert.storage.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("jx_alert.scheduler")
DAILY_COLLECTION_GRACE_SECONDS = 30 * 60


def daily_job(*, now=None):
    """Persist today's due plan inside its grace window for cron or recovery."""
    from jx_alert.operations import enqueue_collection
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning

    try:
        settings = get_settings()
        if settings.app_dry_run:
            return
        clock = now if now is not None else datetime.now(timezone.utc)
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("Scheduled collection requires a timezone-aware clock")
        local = clock.astimezone(ZoneInfo("Asia/Shanghai"))
        midnight = datetime.combine(local.date(), time.min, local.tzinfo)
        earliest = max(midnight, local - timedelta(seconds=DAILY_COLLECTION_GRACE_SECONDS))
        trigger = CronTrigger.from_crontab(effective_daily_sync_cron(settings), timezone=local.tzinfo)
        due = trigger.get_next_fire_time(None, earliest)
        if due is None or due > local:
            return
        day = local.date().isoformat()
        # A fixed Beijing delivery day identifies the plan, while each store's
        # completed local report date is still resolved by the dispatcher. Two
        # sales dates cover a missed previous collection without inventing facts.
        with database_job_lock(engine, "collection_enqueue"), SessionLocal() as db:
            result = enqueue_collection(db, report_date=None, backfill_days=2,
                                        idempotency_key="scheduled-collection:" + day)
            db.commit()
        if not result["reused"]:
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


def hourly_ad_monitor_job():
    from jx_alert.hourly_ad_monitor import run_hourly_monitor
    from jx_alert.storage.job_lock import JobAlreadyRunning
    try:
        result = run_hourly_monitor()
        if result.get("status") not in {"DISABLED", "WAITING", "ALREADY_COMPLETE", "BUSY"}:
            logger.info("Hourly advertising update status=%s", result.get("status"))
    except JobAlreadyRunning:
        return
    except Exception as exc:
        logger.error("Hourly advertising update failed: %s", type(exc).__name__)


def outbox_job():
    from jx_alert.notifications.worker import process_outbox_batch
    result = process_outbox_batch(batch_size=5)
    if result.get("claimed") or result.get("unknown"):
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


def company_executive_digest_job(*, now=None):
    """Queue only the private digest, with minute recovery inside its own window."""
    from jx_alert.config import load_runtime_settings
    from jx_alert.notifications.company_executive import queue_company_digest
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock

    try:
        clock = now if now is not None else datetime.now(timezone.utc)
        if not COMPANY_OPENS <= clock.astimezone(COMPANY_BEIJING).time() < COMPANY_CLOSES:
            return
        settings = load_runtime_settings()
        with database_job_lock(engine, "company_executive_digest"), SessionLocal() as db:
            result = queue_company_digest(db, settings, now=clock)
            db.commit()
        if result not in {"WAITING", "ALREADY_QUEUED", "DISABLED"}:
            logger.info("Company executive digest status=%s", result)
    except JobAlreadyRunning:
        return
    except Exception as exc:
        logger.error("Company executive digest failed: %s", type(exc).__name__)


def sparkx_report_job(*, now=None):
    """08:30 SparkX daily report and 7-day ad review; the switch is re-read at each run."""
    from jx_alert.config import load_runtime_settings
    from jx_alert.sparkx_reports import BEIJING as SPARKX_BEIJING, GENERATE_UNTIL, OPENS, queue_reports
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock

    try:
        clock = now if now is not None else datetime.now(timezone.utc)
        if not OPENS <= clock.astimezone(SPARKX_BEIJING).time() < GENERATE_UNTIL:
            return
        settings = load_runtime_settings()
        with database_job_lock(engine, "sparkx_report"), SessionLocal() as db:
            result = queue_reports(db, settings, now=clock)
            db.commit()
        if result["status"] not in {"DISABLED", "WAITING", "ALREADY_COMPLETE", "RETRY_LATER", "DELIVERY_DISABLED"}:
            logger.info("SparkX report status=%s error=%s queued=%s", result["status"], result.get("error"),
                        len(result.get("outbox_ids") or []))
    except JobAlreadyRunning:
        return
    except Exception as exc:
        logger.error("SparkX report failed: %s", type(exc).__name__)


def main():
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false; scheduler process exits without starting jobs.")
        return

    hourly_mode = getattr(settings, "hourly_ad_monitor_enabled", False)
    collection_cron = effective_daily_sync_cron(settings)
    init_db()
    scheduler = BlockingScheduler(
        timezone=settings.app_timezone,
        executors={"default": ThreadPoolExecutor(max_workers=4),
                   "daily_reports": ThreadPoolExecutor(max_workers=1),
                   "ad_monitor": ThreadPoolExecutor(max_workers=1),
                   "sparkx_reports": ThreadPoolExecutor(max_workers=1)},
    )
    # SparkX network reads run on their own executor so they never delay other lanes.
    scheduler.add_job(sparkx_report_job,
        CronTrigger(hour=COMPANY_OPENS.hour, minute=COMPANY_OPENS.minute, timezone=COMPANY_BEIJING),
        id="sparkx_report", executor="sparkx_reports", replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=1800)
    scheduler.add_job(sparkx_report_job, IntervalTrigger(minutes=5), id="sparkx_report_recovery",
        executor="sparkx_reports", replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=120)
    scheduler.add_job(
        daily_report_job,
        CronTrigger.from_crontab(settings.feishu_daily_report_cron, timezone="Asia/Shanghai"),
        id="feishu_daily_report", executor="daily_reports",
        replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=1800,
    )
    # Always registered: the job re-reads runtime settings, so enabling the lane
    # or choosing its recipient in the console needs no scheduler restart.
    scheduler.add_job(company_executive_digest_job,
        CronTrigger(hour=COMPANY_OPENS.hour, minute=COMPANY_OPENS.minute, timezone=COMPANY_BEIJING),
        id="company_executive_digest", replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=600)
    scheduler.add_job(company_executive_digest_job, IntervalTrigger(minutes=1),
        id="company_executive_digest_recovery", replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=60)
    scheduler.add_job(
        daily_report_job, IntervalTrigger(minutes=1),
        id="feishu_daily_report_recovery", executor="daily_reports",
        replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        daily_job,
        CronTrigger.from_crontab(collection_cron, timezone="Asia/Shanghai"),
        id="daily_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=DAILY_COLLECTION_GRACE_SECONDS,
    )
    scheduler.add_job(
        daily_job, IntervalTrigger(minutes=1),
        id="daily_pipeline_recovery",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60,
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
    if hourly_mode:
        scheduler.add_job(hourly_ad_monitor_job,
            CronTrigger.from_crontab(settings.hourly_ad_monitor_cron, timezone="Asia/Shanghai"),
            id="hourly_ad_monitor", executor="ad_monitor", max_instances=1, coalesce=True,
            misfire_grace_time=1800)
        scheduler.add_job(hourly_ad_monitor_job, IntervalTrigger(minutes=1),
            id="hourly_ad_monitor_recovery", executor="ad_monitor", max_instances=1,
            coalesce=True, misfire_grace_time=60)
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
        collection_cron, settings.feishu_daily_report_cron, settings.app_dry_run,
    )
    # Recover a missing collection plan before starting independent reports and exports.
    daily_job()
    if hourly_mode:
        # Heavy source calls belong to their own executor; starting during the
        # 08:00 window must not block 09:00 delivery or the outbox worker.
        scheduler.add_job(hourly_ad_monitor_job, 'date', run_date=datetime.now(timezone.utc),
            id='hourly_ad_monitor_startup', executor='ad_monitor', max_instances=1)
    # The independent gate prevents pre-08:00 sends and respects the configured
    # first delivery date; persistent idempotency makes restarts safe.
    daily_report_job()
    daily_excel_snapshot_job()
    scheduler.start()


if __name__ == "__main__":
    main()
