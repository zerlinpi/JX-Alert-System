"""Collect append-only SP validation evidence into a separate SQLite file."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
from uuid import uuid4


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--sid", required=True, type=int)
    parser.add_argument("--campaign-id", required=True, type=int)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    days = (args.end - args.start).days + 1
    if args.sid <= 0 or args.campaign_id <= 0 or not 1 <= days <= 60:
        parser.error("Positive IDs and an inclusive date range of 1-60 days are required")
    report_dates = [(args.start + timedelta(days=i)).isoformat() for i in range(days)]
    if not args.collect:
        print(json.dumps({"status": "DRY_RUN", "sid": args.sid,
                          "campaign_id": args.campaign_id, "report_dates": report_dates,
                          "note": "No API, settings, database, or output files accessed"}))
        return 0

    # Import only after explicit collection. Do not initialize the production DB.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.lingxing.sp_validation import collect_sp_validation
    from jx_alert.storage.models import RawSnapshot

    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    evidence_path = root / "sp-validation.sqlite"
    engine = create_engine("sqlite:///" + evidence_path.as_posix())
    RawSnapshot.__table__.create(engine, checkfirst=True)
    client = LingxingClient.from_settings()
    with Session(engine) as db:
        result = collect_sp_validation(client, db, sid=args.sid,
                                       campaign_id=args.campaign_id, report_dates=report_dates)
        db.commit()
    # New report per run; neither evidence versions nor existing reports replaced.
    path = root / ("sp-validation-" + uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"status": result["status"], "summary_file": str(path),
                      "evidence_database": str(evidence_path)}, ensure_ascii=False))
    return 1 if result["status"] in {"FAILED", "PARTIAL"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
