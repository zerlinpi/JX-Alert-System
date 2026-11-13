"""Evidence-only daily comparison selection shared by query and review tools."""
from datetime import timedelta
import math


def metric_comparison(values, target, complete_dates, *, currencies=None):
    def observed(day):
        value = values.get(day)
        if day not in complete_dates or value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    current = observed(target)
    previous_date = target - timedelta(days=1)
    previous = observed(previous_date)
    days = [target - timedelta(days=i) for i in range(1, 8)]
    baseline = [observed(day) for day in days]
    baseline_complete = all(value is not None for value in baseline)
    average = None
    if baseline_complete:
        try:
            average = math.fsum(baseline) / 7
        except OverflowError:
            average = math.fsum(value / 7 for value in baseline)
    if average is not None and not math.isfinite(average):
        average, baseline_complete = None, False

    def same_currency(on):
        if currencies is None:
            return True
        codes = [currencies.get(day) for day in on]
        return (all(isinstance(code, str) and len(code) == 3 and code.isascii() and code.isupper()
                    and code.isalpha() for code in codes) and len(set(codes)) == 1)

    previous_valid = same_currency([target, previous_date])
    baseline_valid = same_currency([target, *days])
    basis, label, reference = "INSUFFICIENT_DATA", "数据不足", None
    if current is not None and baseline_complete and baseline_valid:
        basis, label, reference = "SEVEN_DAY", "较过去7日均值", average
    elif current is not None and previous is not None and previous_valid:
        basis, label, reference = "PREVIOUS_DAY", "较前一日", previous
    elif current is not None and (baseline_complete or previous is not None) and currencies is not None:
        basis, label = "CURRENCY_UNCONFIRMED", "币种待确认或不一致"

    def change(reference, valid=True):
        result = ((current - reference) / reference if current is not None and reference is not None
                  and reference > 0 and valid else None)
        return result if result is not None and math.isfinite(result) else None

    return {"current": current, "previous_day": previous,
            "change_vs_previous_day": change(previous, previous_valid),
            "average_7d": average if baseline_valid else None,
            "change_vs_7d_average": change(average, baseline_valid),
            "baseline_complete": baseline_complete,
            "comparison_basis": basis, "comparison_label": label,
            "comparison_value": reference, "change_ratio": change(reference),
            "comparison_dates": ([target.isoformat(), *[d.isoformat() for d in days]] if basis == "SEVEN_DAY"
                                 else [target.isoformat(), previous_date.isoformat()] if basis == "PREVIOUS_DAY" else [])}
