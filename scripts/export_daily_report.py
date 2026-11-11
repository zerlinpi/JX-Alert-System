"""Export stored JX Alert data to consolidated and per-store Excel workbooks."""
import argparse
from datetime import date

from jx_alert.config import get_settings
from jx_alert.reporting.excel import export_daily_reports
from jx_alert.storage.db import SessionLocal, init_db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Report date YYYY-MM-DD.")
    parser.add_argument("--store-id", help="Optional Lingxing sid for one store workbook.")
    parser.add_argument("--output-dir", help="Override REPORT_DIR for this export.")
    args = parser.parse_args()
    try:
        report_date = date.fromisoformat(args.date)
    except ValueError as exc:
        raise SystemExit("--date must be YYYY-MM-DD") from exc

    settings = get_settings()
    init_db()
    with SessionLocal() as db:
        paths = export_daily_reports(
            db,
            report_date,
            args.output_dir or settings.report_dir,
            store_id=args.store_id,
        )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
