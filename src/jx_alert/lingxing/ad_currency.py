"""Resolve monetary advertising facts from exact seller account identities."""
from collections import defaultdict
import re


def is_ad_money(metric):
    return metric == "spend" or "sales" in metric.split("_")


def account_currencies(accounts):
    by_profile = defaultdict(set)
    for row in accounts:
        sid, profile = row.get("sid"), row.get("profile_id")
        if not sid or not profile or row.get("type") != "seller":
            continue
        code = row.get("currency_code")
        by_profile[str(sid), str(profile)].add(
            code if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code) else None
        )
    return {key: next(iter(codes)) for key, codes in by_profile.items() if len(codes) == 1 and None not in codes}


def advertising_currency(currencies, store_id, report_row, *, campaign_profile=None):
    profile = report_row.get("profile_id")
    if profile and campaign_profile and str(profile) != str(campaign_profile):
        return None
    profile = profile or campaign_profile
    return currencies.get((str(store_id), str(profile))) if profile else None
