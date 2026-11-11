"""Validation helpers for store-local report-date windows."""
from datetime import date


def normalize_store_report_dates(
    report_dates: str | dict[str, str | list[str]],
    store_id: str,
) -> list[str]:
    value = report_dates if isinstance(report_dates, str) else report_dates.get(str(store_id))
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list) and value:
        values = value
    else:
        raise ValueError(f"Missing report date(s) for store sid={store_id}")

    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"Report dates for sid={store_id} must be ISO date strings")
        try:
            date.fromisoformat(item)
        except ValueError as exc:
            raise ValueError(
                f"Invalid report date for sid={store_id}: {item!r}"
            ) from exc
        if item not in normalized:
            normalized.append(item)
    if not normalized:
        raise ValueError(f"Empty report date window for store sid={store_id}")
    return normalized
