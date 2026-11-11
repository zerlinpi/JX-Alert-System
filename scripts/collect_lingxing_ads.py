"""Dry-run by default. --collect performs explicit read-only SP/SB/SD report collection."""
import argparse
import json
from pathlib import Path

from jx_alert.config import get_settings
from jx_alert.lingxing.client import LingxingClient
from jx_alert.lingxing.collector import collect_ads_for_day
from jx_alert.storage.db import SessionLocal, init_db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true",
                        help="Fetch read-only Lingxing SP/SB/SD campaign reports.")
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument("--date", help="One YYYY-MM-DD date for all selected stores.")
    date_group.add_argument("--date-map", help="JSON file mapping each Lingxing sid to its local date.")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.lingxing_app_id or not settings.lingxing_app_secret:
        raise SystemExit("LINGXING_APP_ID and LINGXING_APP_SECRET must be configured locally.")
    if not args.collect:
        print("DRY RUN: credentials are present; no token or store/report API request was made.")
        print("Use --collect with --date or --date-map only after token contracts are verified.")
        return
    if settings.app_dry_run:
        raise SystemExit("APP_DRY_RUN=true. Set APP_DRY_RUN=false locally before explicit --collect.")
    if not args.date and not args.date_map:
        raise SystemExit("Provide --date or --date-map with --collect.")

    if args.date_map:
        report_dates = json.loads(Path(args.date_map).read_text(encoding="utf-8"))
        if not isinstance(report_dates, dict):
            raise SystemExit("--date-map JSON must be an object: {\\"sid\\": \\"YYYY-MM-DD\\"}")
    else:
        report_dates = args.date

    init_db()
    client = LingxingClient.from_settings()
    with SessionLocal() as db:
        summary = collect_ads_for_day(client, db, report_dates)
    print("Read-only Lingxing ads collection completed:")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
