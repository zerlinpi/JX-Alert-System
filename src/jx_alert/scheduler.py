import logging

from apscheduler.schedulers.blocking import BlockingScheduler
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
    settings = get_settings()
    if settings.app_dry_run:
        logger.info("DRY RUN: daily collection/anomaly pipeline not executed.")
        return
    # Intentionally fail closed until the endpoint-specific collection adapters,
    # response schemas and historical backfill have been verified.
    logger.warning(
        "Live collection is not enabled: the verified data pipeline is not configured. "
        "No Lingxing API request was sent."
    )


def outbox_job():
    from jx_alert.notifications.worker import process_outbox_batch
    result = process_outbox_batch(batch_size=5)
    if result.get("claimed"):
        logger.info("Feishu outbox batch: %s", result)


def main():
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false; scheduler process exits without starting jobs.")
        return

    init_db()
    scheduler = BlockingScheduler(timezone=settings.app_timezone)
    scheduler.add_job(
        daily_job,
        CronTrigger.from_crontab(settings.daily_sync_cron, timezone=settings.app_timezone),
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
    logger.info(
        "Scheduler started (timezone=%s, cron=%s, dry_run=%s)",
        settings.app_timezone, settings.daily_sync_cron, settings.app_dry_run,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
