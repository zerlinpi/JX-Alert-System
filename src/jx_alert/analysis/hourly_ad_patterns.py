"""Comparable mature campaign patterns. These are review triggers, not margin estimates."""
from datetime import timedelta
import math


HIGH_LOW = "high_spend_low_conversion"
LOW_ZERO = "low_spend_zero_conversion"
OPPORTUNITY = "low_spend_high_conversion"
PATTERN_SIGNALS = {HIGH_LOW, LOW_ZERO, OPPORTUNITY}


def _observed(row):
    fields = ("spend", "orders", "sales", "clicks", "units")
    if (not row or row.get("currency") != "USD" or row.get("sales_currency") != "USD"
            or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k])
                   or row[k] < 0 for k in fields)):
        return False
    endpoints = [row[k] > 0 for k in ("orders", "sales", "units")]
    return len(set(endpoints)) == 1 and row["orders"] <= row["clicks"]


def _comparison(current, reference, *, dates, reference_days):
    if not (all(_observed(row) for row in (*current, *reference))
            and sum(row["orders"] for row in reference) >= max(5, len(reference))):
        return None
    c = {key: math.fsum(row[key] for row in current) for key in ("clicks", "spend", "orders", "sales")}
    r = {key: math.fsum(row[key] for row in reference) for key in c}
    if not all(math.isfinite(v) for v in (*c.values(), *r.values())):
        return None
    if not c["clicks"] or not c["spend"] or not r["clicks"] or not r["spend"]:
        return None
    rate = r["orders"] / r["clicks"]
    expected = c["clicks"] * rate
    reference_cpa = r["spend"] / r["orders"]
    return {
        **c, "reference_dates": [x.isoformat() for x in reference_days],
        "dates": [x.isoformat() for x in dates], "expected_orders": expected,
        "estimated_excess_spend": c["spend"] - c["orders"] * reference_cpa,
        "cpa": c["spend"] / c["orders"] if c["orders"] else None,
        "reference_cpa": reference_cpa, "roas": c["sales"] / c["spend"],
        "reference_roas": r["sales"] / r["spend"],
    }


def classify_hourly_ad_pattern(observed, mature_day, reference_days, skill):
    """Return one proven spend/conversion pattern without cross-store/source aggregation."""
    high = max(50, skill["min_spend_usd"])
    clicks = max(30, skill["min_clicks"])
    current = observed.get(mature_day)
    if not _observed(current):
        return None
    if current["spend"] >= high:
        if current["clicks"] < clicks or current["orders"] <= 0:
            return None  # established per-day zero-conversion rule handles explicit zero
        baseline = [observed.get(day) for day in reference_days]
        entry = _comparison([current], baseline, dates=[mature_day], reference_days=reference_days)
        min_expected = skill["min_expected_orders"] * (1.5 if len(baseline) != 7 else 1)
        if (entry and entry["expected_orders"] >= min_expected
                and entry["expected_orders"] - current["orders"] >= 3
                and current["orders"] <= entry["expected_orders"] * (1 - skill["min_relative_change"])
                and entry["estimated_excess_spend"] >= 30):
            return {"code": HIGH_LOW, **entry}

    # A small amount or a single day never qualifies. These days are already
    # independently mature and complete, and the baseline never overlaps them.
    dates = [mature_day - timedelta(days=x) for x in (2, 1, 0)]
    days = [observed.get(day) for day in dates]
    if not all(_observed(row) and 0 < row["spend"] < high for row in days):
        return None
    older = [dates[0] - timedelta(days=x) for x in range(7, 0, -1)]
    baseline = [observed.get(day) for day in older]
    entry = _comparison(days, baseline, dates=dates, reference_days=older)
    if not entry or entry["clicks"] < clicks:
        return None
    if (entry["orders"] == 0 and entry["spend"] >= high
            and entry["expected_orders"] >= skill["min_expected_orders"]
            and entry["spend"] >= 2 * entry["reference_cpa"]):
        return {"code": LOW_ZERO, **entry}
    if (skill["objective"] in {"balanced", "efficiency"} and entry["spend"] >= 15
            and entry["orders"] >= 5 and sum(row["orders"] > 0 for row in days) >= 2
            and entry["orders"] >= entry["expected_orders"] * 1.3
            and entry["cpa"] <= .7 * entry["reference_cpa"]
            and entry["roas"] >= max(entry["reference_roas"] * 1.3, entry["reference_roas"] + .5)):
        return {"code": OPPORTUNITY, **entry}
    return None
