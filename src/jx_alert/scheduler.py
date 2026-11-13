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


def daily_job():
    from jx_alert.operations import run_collection
    try:
        result = run_collection()
        logger.info("Daily collection status=%s", result["status"])
    except Exception as exc:
        logger.error("Daily collection failed: %s", type(exc).__name__)


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
    logger.info(
        "Scheduler started (daily_timezone=Asia/Shanghai, collection_cron=%s, report_cron=%s, dry_run=%s)",
        settings.daily_sync_cron, settings.feishu_daily_report_cron, settings.app_dry_run,
    )
    # The independent gate prevents pre-08:00 sends and respects the configured
    # first delivery date; persistent idempotency makes restarts safe.
    daily_report_job()
    scheduler.start()


if __name__ == "__main__":
    main()
