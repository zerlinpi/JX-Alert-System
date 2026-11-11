import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jx_alert.config import get_settings
from jx_alert.storage.db import SessionLocal, init_db
from jx_alert.lingxing.client import LingxingClient
from jx_alert.lingxing.pipeline import collect_daily_snapshot
from jx_alert.analysis.evaluator import evaluate_daily_anomalies
from jx_alert.storage.store_config import build_store_report_dates, sync_store_config
from jx_alert.reporting.excel import export_daily_reports

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

    try:
        init_db()
        client = LingxingClient.from_settings()
        stores_response = client.get_stores()
        report_dates = build_store_report_dates(
            stores_response, settings.store_timezones_file
        )
        if not report_dates:
            logger.warning("No active Lingxing stores found; daily pipeline skipped.")
            return

        with SessionLocal() as db:
            collection = collect_daily_snapshot(
                client,
                db,
                report_dates,
                stores_response=stores_response,
                ad_attribution_refresh_days=settings.ad_attribution_refresh_days,
            )
            configured_stores = sync_store_config(db, settings.store_timezones_file)
            db.commit()
            # Evaluate each store against its own marketplace-local report date.
            analyses = [evaluate_daily_anomalies(db, report_dates)]
            report_files = []
            for sid, report_date in report_dates.items():
                report_files.extend(str(path) for path in export_daily_reports(
                    db, report_date, settings.report_dir, store_id=sid
                ))

        logger.info(
            "Daily pipeline complete: job_id=%s stores=%s timezone_configs=%s "
            "analyses=%s report_files=%s",
            collection["job_id"], collection["store_count"], configured_stores,
            analyses, report_files,
        )
    except Exception:
        logger.exception("Daily Lingxing collection/analysis failed; no partial data is treated as complete.")


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
