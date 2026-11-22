"""Offline owner daily card preview; the sample never reads or sends live data."""

from datetime import date, datetime, timezone
import json

from jx_alert.notifications.owner_daily_card import build_owner_daily_cards


def sample():
    return build_owner_daily_cards([
        {"store_id": "101", "store_name": "示例店铺 A", "currency": "USD", "ad_spend": 310,
         "covered_ad_types": ["SP", "SB", "SBV", "SD", "共享分摊"],
         "sales": 1350, "return_units": 3, "sold_units": 80, "refund_amount": 145},
        {"store_id": "102", "store_name": "示例店铺 B", "currency": "USD", "ad_spend": 0,
         "covered_ad_types": ["SP", "SB", "SBV", "SD", "共享分摊"],
         "sales": 418, "return_units": 0, "sold_units": 20, "refund_amount": None},
    ], report_date=date(2026, 11, 20), as_of=datetime(2026, 11, 22, 0, 25, tzinfo=timezone.utc))


if __name__ == "__main__":
    print(json.dumps({"example_only": True, "cards": sample()}, ensure_ascii=False, indent=2))
