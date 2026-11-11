"""Read-only daily Amazon ads collection using verified Lingxing report contracts."""
from datetime import date

from sqlalchemy import delete

from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import DailyMetric, RawSnapshot


AD_METRICS = {
    "cost": "spend",
    "orders": "orders",
    "sales": "sales",
    "units": "units",
    "clicks": "clicks",
    "impressions": "impressions",
}


def collect_ads_for_day(client, db, report_date: str | dict[str, str], *,
                        stores_response: dict | None = None) -> dict:
    """Fetch store list and SP/SB/SD campaign reports, storing raw + normalized data.

    The caller must supply the store-local report date. This function deliberately
    does not guess marketplace time zones or trigger itself on a schedule.
    """
    if isinstance(report_date, str):
        try:
            date.fromisoformat(report_date)
        except ValueError as exc:
            raise ValueError("report_date must be YYYY-MM-DD") from exc

    def store_report_date(sid) -> str:
        value = report_date if isinstance(report_date, str) else report_date.get(str(sid))
        if not value:
            raise ValueError(f"Missing store-local report date for sid={sid}")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid store-local report date for sid={sid}") from exc
        return value

    stores_response = stores_response or client.get_stores()
    stores = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(stores, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")
    db.add(RawSnapshot(source="lingxing_seller_list", payload=stores_response))
    db.commit()

    active_ad_stores = [
        store for store in stores
        if int(store.get("status") or 0) == 1
        and int(store.get("has_ads_setting") or 0) == 1
    ]
    report_counts = {"sp": 0, "sb": 0, "sd": 0}
    for store in active_ad_stores:
        sid = store.get("sid")
        if sid is None:
            raise RuntimeError("Active seller-list record is missing sid")
        store_id = str(sid)
        local_report_date = store_report_date(sid)
        metric_date = date.fromisoformat(local_report_date)

        for report_type in ("sp", "sb", "sd"):
            payload = {"sid": sid, "report_date": local_report_date, "length": 100}
            if report_type in {"sp", "sd"}:
                payload["show_detail"] = 1
            rows = list(client.iter_advertising_report(report_type, payload))

            dimension = f"campaign_{report_type}"
            db.execute(
                delete(DailyMetric).where(
                    DailyMetric.store_id == store_id,
                    DailyMetric.metric_date == metric_date,
                    DailyMetric.dimension == dimension,
                )
            )
            db.add(RawSnapshot(
                source=f"lingxing_ads_{report_type}_campaign_report",
                store_id=store_id,
                payload={
                    "report_type": report_type,
                    "report_date": local_report_date,
                    "rows": rows,
                },
            ))

            for row in rows:
                campaign_id = row.get("campaign_id")
                if campaign_id is None:
                    raise RuntimeError(
                        f"{report_type.upper()} report row is missing campaign_id"
                    )
                row_date = row.get("report_date")
                if row_date and row_date != local_report_date:
                    raise RuntimeError(
                        f"{report_type.upper()} report returned unexpected report_date"
                    )
                for source_field, metric_name in AD_METRICS.items():
                    value = row.get(source_field)
                    if value is None:
                        continue
                    try:
                        numeric_value = float(value)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            f"Invalid {source_field} value in {report_type.upper()} report"
                        ) from exc
                    db.add(DailyMetric(
                        store_id=store_id,
                        metric_date=metric_date,
                        dimension=dimension,
                        entity_id=str(campaign_id),
                        metric_name=metric_name,
                        value=numeric_value,
                        currency=None,
                        source=f"lingxing_ads_{report_type}",
                    ))
            mark_coverage(
                db,
                store_id=store_id,
                metric_date=date.fromisoformat(local_report_date),
                dimension=f"campaign_{report_type}",
                row_count=len(rows),
            )
            db.commit()
            report_counts[report_type] += len(rows)

    return {
        "report_date": report_date if isinstance(report_date, str) else "per-store mapping",
        "stores_seen": len(stores),
        "active_ad_stores": len(active_ad_stores),
        "rows": report_counts,
    }
