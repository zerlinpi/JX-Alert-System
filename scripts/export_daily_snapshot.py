"""Export all persisted business dimensions without contacting Lingxing."""
import argparse
import json

from jx_alert.reporting.daily_job import run_daily_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-date", required=True,
                        help="Beijing archive date YYYY-MM-DD; keeps each source report date intact.")
    args = parser.parse_args()
    result = run_daily_snapshot(snapshot_date=args.snapshot_date)
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result["status"] not in {"COMPLETE", "ALREADY_COMPLETE"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
