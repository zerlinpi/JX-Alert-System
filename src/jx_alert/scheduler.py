import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from jx_alert.config import get_settings

logging.basicConfig(level=logging.INFO)
def daily_job():
    # Deliberately no live sync until credentials, mappings and verified payload contracts exist.
    logging.info("Daily pipeline placeholder: configure and validate Lingxing adapters before enabling.")

def main():
    s = get_settings()
    if s.app_dry_run:
        logging.warning("APP_DRY_RUN=true; scheduler will not run live data collection.")
    scheduler = BlockingScheduler(timezone=s.app_timezone)
    scheduler.add_job(daily_job, CronTrigger.from_crontab("0 8 * * *", timezone=s.app_timezone),
                      id="daily_pipeline", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()

if __name__ == "__main__": main()
