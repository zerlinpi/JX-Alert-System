"""Explicit opt-in 8-30 day historical backfill for complete daily baselines."""
import argparse
import json
from datetime import date, timedelta

from jx_alert.analysis.evaluator import evaluate_daily_anomalies
from jx_alert.config import get_settings
from jx_alert.lingxing.client import LingxingClient
from jx_alert.lingxing.pipeline import collect_daily_snapshot
from jx_alert.storage.db import SessionLocal, init_db
from jx_alert.storage.store_config import build_store_report_dates, sync_store_config
from jx_alert.reporting.excel import export_daily_reports


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true",
                        help="Fetch read-only historical sales and advertising reports.")
    parser.add_argument("--days", type=int, default=14,
                        help="Number of completed local report dates to backfill (8-30).")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.lingxing_app_id or not settings.lingxing_app_secret:
        raise SystemExit("LINGXING_APP_ID and LINGXING_APP_SECRET must be configured locally.")
    if not args.collect:
        print("DRY RUN: no Lingxing token or business API request was made.")
        print("Use --collect --days 14 after verifying token settings and store timezones.")
        return
    if settings.app_dry_run:
        raise SystemExit("APP_DRY_RUN=true. Set APP_DRY_RUN=false locally before explicit --collect.")
    if not 8 <= args.days <= 30:
        raise SystemExit("--days must be between 8 and 30 to build a seven-day baseline.")

    init_db()
    client = LingxingClient.from_settings()
    stores_response = client.get_stores()
    base_dates = build_store_report_dates(
        stores_response, settings.store_timezones_file
    )
    if not base_dates:
        raise SystemExit("No active Lingxing stores were returned.")

    analysis_dates: set[str] = set()
    analysis_maps: list[dict[str, str]] = []
    history_maps = []
    collected = []
    with SessionLocal() as db:
        for offset in range(args.days - 1, -1, -1):
            report_dates = {
                sid: (date.fromisoformat(base_date) - timedelta(days=offset)).isoformat()
                for sid, base_date in base_dates.items()
            }
            result = collect_daily_snapshot(
                client, db, report_dates, stores_response=stores_response
            )
            collected.append(result)
            history_maps.append(report_dates)
            sync_store_config(db, settings.store_timezones_file)
            db.commit()
            if offset <= args.days - 8:
                analysis_maps.append(report_dates)
                analysis_dates.update(report_dates.values())

        # A backfill day is a per-store date map, not necessarily one global date.
        # Keep each store aligned to its own marketplace calendar.
        analyses = [
            evaluate_daily_anomalies(db, date_map)
            for date_map in analysis_maps
        ]
        report_files = []
        for date_map in history_maps:
            for sid, report_date in date_map.items():
                report_files.extend(str(path) for path in export_daily_reports(
                    db, report_date, settings.report_dir, store_id=sid
                ))

    print(json.dumps({
        "status": "COMPLETED",
        "days_requested": args.days,
        "daily_jobs": len(collected),
        "analysis_dates": sorted(analysis_dates),
        "analysis_days": len(analysis_maps),
        "analyses": analyses,
        "report_files": report_files,
    }, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
