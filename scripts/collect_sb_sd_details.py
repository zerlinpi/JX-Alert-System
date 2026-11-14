"""Collect supplemental SB/SD reports for selected completed store dates."""
import argparse
from datetime import date
import json

from sqlalchemy import func, select

from jx_alert.config import get_settings
from jx_alert.lingxing.ad_currency import account_currencies
from jx_alert.lingxing.ad_details import collect_ad_details
from jx_alert.lingxing.client import LingxingClient
from jx_alert.operations import scoped_stores, store_report_plan
from jx_alert.storage.db import SessionLocal, engine
from jx_alert.storage.job_lock import database_job_lock
from jx_alert.storage.models import RawSnapshot
from jx_alert.storage.raw_archive import archive_raw_snapshots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", action="append", required=True, help="Configured store ID; repeat to select more")
    parser.add_argument("--date", action="append", required=True, type=date.fromisoformat,
                        help="Completed report date YYYY-MM-DD; repeat for up to seven dates")
    args = parser.parse_args()
    ids = set(args.store)
    days = sorted({d.isoformat() for d in args.date})
    if any(not sid.isascii() or not sid.isdigit() or int(sid) <= 0 for sid in ids) or len(days) > 7:
        parser.error("Provide positive store IDs and at most seven distinct report dates")
    settings = get_settings()
    if settings.app_dry_run or not settings.sb_sd_detail_reports_enabled:
        raise SystemExit("SB/SD detail collection is disabled")
    with database_job_lock(engine), SessionLocal() as db:
        client = LingxingClient.from_settings()
        scoped = scoped_stores(client.get_stores(), settings.collection_scope_file, allow_missing=True)
        stores = [r for r in scoped["data"] if str(r["sid"]) in ids and int(r.get("has_ads_setting") or 0) == 1]
        plan = store_report_plan(stores, settings.store_timezones_file,
            allow_unknown_timezone_history=settings.allow_unconfigured_timezone_history)
        ready = {r["store_id"]: r["report_date"] for r in plan if r["state"] == "READY"}
        if set(ready) != ids or any(days[-1] > latest for latest in ready.values()):
            raise SystemExit("All selected stores must be in scope, authorized and have completed the requested dates")
        previous_snapshot_id = db.scalar(select(func.max(RawSnapshot.id))) or 0
        result = collect_ad_details(client, db, {sid: days for sid in ready},
                                   stores_response={"data": stores},
                                   ad_currencies=account_currencies(client.seller_ad_accounts()))
        archive_raw_snapshots(db, settings.raw_dir, after_id=previous_snapshot_id)
    print(json.dumps(result, ensure_ascii=False))
    if result["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(type(exc).__name__) from None
