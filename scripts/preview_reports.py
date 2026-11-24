"""Print what a lane would send now, as plain text. Read-only: nothing is queued or sent.

DeepSeek queries SparkX itself, so a run takes a few minutes and uses DeepSeek tokens.
"""
import argparse
from datetime import datetime, timezone
import json

from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.config import load_runtime_settings
from jx_alert.notifications.cards import plain_text
from jx_alert.reports.lanes import LANES, preview
from jx_alert.sparkx.client import SparkXError
from jx_alert.storage.db import SessionLocal, init_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--json", action="store_true", help="print the Feishu card JSON as well")
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        try:
            cards, info = preview(load_runtime_settings(), args.lane, now=datetime.now(timezone.utc), db=db)
        except SparkXError as exc:
            raise SystemExit(exc.message) from None
        except DeepSeekError as exc:
            raise SystemExit(f"DeepSeek: {exc}") from None
        db.commit()  # Only the SparkX roster snapshot is stored.
    for card in cards:
        print(plain_text(card))
        print("=" * 40)
        if args.json:
            print(json.dumps(card, ensure_ascii=False))
    if not cards:
        print("No card: nothing to report.")
    print(json.dumps(info, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
