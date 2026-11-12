"""Run the configured read-only collection scope; no external sends here."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--report-date")
    parser.add_argument("--backfill-days", type=int, default=1)
    args = parser.parse_args()
    if not args.collect:
        print('{"status":"DRY_RUN"}')
        return
    from jx_alert.operations import run_collection
    try:
        result = run_collection(report_date=args.report_date, backfill_days=args.backfill_days)
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result["status"] not in {"COMPLETE", "DRY_RUN"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
