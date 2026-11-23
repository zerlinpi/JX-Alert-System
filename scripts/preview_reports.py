"""Print the card a lane would send now, as plain text. Read-only: nothing is queued or sent."""
import argparse
from datetime import datetime, timezone

from jx_alert.config import load_runtime_settings
from jx_alert.notifications.cards import plain_text
from jx_alert.reports.lanes import LANES, preview
from jx_alert.sparkx.client import SparkXError
from jx_alert.storage.db import SessionLocal, init_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=LANES)
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        try:
            card, info = preview(load_runtime_settings(), args.lane, now=datetime.now(timezone.utc), db=db)
        except SparkXError as exc:
            raise SystemExit(exc.message) from None
        db.commit()  # Only the SparkX roster snapshot is stored.
    print(plain_text(card) if card else "No card: nothing to report or the card exceeds Feishu limits.")
    print({key: value for key, value in info.items() if key != "errors"} | {"errors": sorted(info["errors"])})


if __name__ == "__main__":
    main()
