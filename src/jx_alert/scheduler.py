"""Scheduler: 08:30 daily report and review, hourly monitor, outbox worker (Beijing time).

Each job re-reads the owner's switches on every run, so enabling a lane or
choosing a recipient in the console needs no restart. SparkX reads share one
single-thread executor; the outbox worker runs independently every 10 seconds.
"""
from datetime import datetime, timezone
import logging

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jx_alert.config import get_settings, load_runtime_settings
from jx_alert.storage.db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("jx_alert.scheduler")
_QUIET = {"DISABLED", "WAITING", "ALREADY_COMPLETE", "RETRY_LATER", "DELIVERY_DISABLED", "OUTSIDE_HOURS",
          "NOT_SCHEDULED_TODAY", "NO_RECIPIENT", "NO_FINDINGS"}


def _deliver(ids):
    """Send just-queued rows now, in bounded batches, without draining unrelated backlog."""
    from jx_alert.notifications.worker import process_outbox_batch

    ids = [value for value in dict.fromkeys(ids or []) if type(value) is int and value > 0]
    sent = 0
    for offset in range(0, len(ids), 5):
        sent += process_outbox_batch(batch_size=5, outbox_ids=ids[offset:offset + 5]).get("sent", 0)
    return sent


def morning_job(*, now=None):
    """08:30–10:00 Beijing: the daily report and the 7-day review, each independently."""
    from jx_alert.reports.lanes import BEIJING, GENERATE_UNTIL, OPENS, queue_morning_lane
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock

    clock = now or datetime.now(timezone.utc)
    if not OPENS <= clock.astimezone(BEIJING).time() < GENERATE_UNTIL:
        return
    settings = load_runtime_settings()
    for lane in ("daily", "review"):
        try:
            with database_job_lock(engine, f"jx_{lane}"), SessionLocal() as db:
                result = queue_morning_lane(db, settings, lane, now=clock)
                db.commit()
            if result["status"] not in _QUIET:
                logger.info("Lane %s status=%s error=%s queued=%s sent=%s", lane, result["status"],
                            result.get("error"), len(result.get("outbox_ids") or []), _deliver(result.get("outbox_ids")))
        except JobAlreadyRunning:
            continue
        except Exception as exc:  # noqa: BLE001 - one lane must not stop the other.
            logger.error("Lane %s failed: %s", lane, type(exc).__name__)


def hourly_job(*, now=None):
    from jx_alert.reports.lanes import run_hourly
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock

    clock = now or datetime.now(timezone.utc)
    try:
        with database_job_lock(engine, "jx_hourly"), SessionLocal() as db:
            result = run_hourly(db, load_runtime_settings(), now=clock)
            db.commit()
        if result["status"] not in _QUIET:
            logger.info("Hourly monitor status=%s error=%s queued=%s sent=%s", result["status"], result.get("error"),
                        len(result.get("outbox_ids") or []), _deliver(result.get("outbox_ids")))
    except JobAlreadyRunning:
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("Hourly monitor failed: %s", type(exc).__name__)


def outbox_job():
    from jx_alert.notifications.worker import process_outbox_batch

    try:
        result = process_outbox_batch(batch_size=5)
        if result.get("claimed") or result.get("unknown"):
            logger.info("Feishu outbox batch: %s", result)
    except Exception as exc:  # noqa: BLE001
        logger.error("Feishu outbox failed: %s", type(exc).__name__)


def main():
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false; scheduler process exits without starting jobs.")
        return
    init_db()
    scheduler = BlockingScheduler(timezone="Asia/Shanghai", executors={
        "default": ThreadPoolExecutor(max_workers=2), "sparkx": ThreadPoolExecutor(max_workers=1)})
    beijing = "Asia/Shanghai"
    scheduler.add_job(morning_job, CronTrigger(hour=8, minute=30, timezone=beijing), id="morning",
                      executor="sparkx", max_instances=1, coalesce=True, misfire_grace_time=1800)
    # Recovery inside 08:30–10:00 after a restart, a SparkX failure or a late recipient change.
    scheduler.add_job(morning_job, IntervalTrigger(minutes=5), id="morning_recovery", executor="sparkx",
                      max_instances=1, coalesce=True, misfire_grace_time=120)
    scheduler.add_job(hourly_job, CronTrigger(minute=5, timezone=beijing), id="hourly", executor="sparkx",
                      max_instances=1, coalesce=True, misfire_grace_time=1200)
    scheduler.add_job(hourly_job, IntervalTrigger(minutes=10), id="hourly_recovery", executor="sparkx",
                      max_instances=1, coalesce=True, misfire_grace_time=120)
    scheduler.add_job(outbox_job, IntervalTrigger(seconds=10), id="feishu_outbox", max_instances=1, coalesce=True,
                      misfire_grace_time=30)
    logger.info("Scheduler started (Beijing time, dry_run=%s, delivery=%s)", settings.app_dry_run,
                settings.delivery_enabled())
    scheduler.start()


if __name__ == "__main__":
    main()
