"""Read-only daily Amazon ads collection using verified Lingxing report contracts."""
from datetime import date

from sqlalchemy import delete, select

from jx_alert.lingxing.ad_currency import advertising_currency, is_ad_money
from jx_alert.lingxing.report_dates import normalize_store_report_dates
from jx_alert.lingxing.report_metrics import AD_REPORT_METRICS
from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import Campaign, DailyMetric, RawSnapshot


AD_METRICS = AD_REPORT_METRICS

def collect_ads_for_day(client, db,
                        report_date: str | dict[str, str | list[str]], *,
                        stores_response: dict | None = None, ad_currencies: dict | None = None) -> dict:
    """Fetch SP/SB/SD campaign reports for one or more store-local dates.

    The date map may contain a single ISO date or an ordered list per sid. The
    pipeline uses the latter to refresh the attribution window used by the
    rolling seven-day baseline.
    """
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
        local_report_dates = normalize_store_report_dates(report_date, store_id)
        profiles = {(row.campaign_type, row.campaign_id): row.profile_id for row in db.scalars(
            select(Campaign).where(Campaign.store_id == store_id)
        )} if ad_currencies else {}

        for local_report_date in local_report_dates:
            metric_date = date.fromisoformat(local_report_date)
            for report_type in ("sp", "sb", "sd"):
                payload = {
                    "sid": sid,
                    "report_date": local_report_date,
                    "length": 100,
                }
                if report_type in {"sp", "sd"}:
                    payload["show_detail"] = 1
                rows = list(client.iter_advertising_report(report_type, payload))

                normalized = []
                for row in rows:
                    campaign_id = row.get("campaign_id")
                    if campaign_id is None:
                        raise RuntimeError(
                            f"{report_type.upper()} report row is missing campaign_id"
                        )
                    if row.get("report_date") != local_report_date:
                        raise RuntimeError(
                            f"{report_type.upper()} report returned an unexpected report_date"
                        )
                    values = {}
                    for source_field, metric_name in AD_METRICS.items():
                        value = row.get(source_field)
                        if value is None:
                            if source_field in {
                                "cost", "orders", "sales", "units", "clicks", "impressions"
                            }:
                                raise RuntimeError(
                                    f"{report_type.upper()} report row is missing required "
                                    f"metric {source_field}"
                                )
                            continue
                        try:
                            values[metric_name] = float(value)
                        except (TypeError, ValueError) as exc:
                            raise RuntimeError(
                                f"Invalid {source_field} value in {report_type.upper()} report"
                            ) from exc
                    code = advertising_currency(ad_currencies or {}, store_id, row,
                        campaign_profile=profiles.get((report_type, str(campaign_id))))
                    normalized.append((str(campaign_id), values, code))

                dimension = f"campaign_{report_type}"
                db.execute(delete(DailyMetric).where(
                    DailyMetric.store_id == store_id,
                    DailyMetric.metric_date == metric_date,
                    DailyMetric.dimension == dimension,
                ))
                db.add(RawSnapshot(
                    source=f"lingxing_ads_{report_type}_campaign_report",
                    store_id=store_id,
                    payload={
                        "report_type": report_type,
                        "report_date": local_report_date,
                        "rows": rows,
                    },
                ))

                for campaign_id, values, code in normalized:
                    for metric_name, numeric_value in values.items():
                        db.add(DailyMetric(
                            store_id=store_id,
                            metric_date=metric_date,
                            dimension=dimension,
                            entity_id=campaign_id,
                            metric_name=metric_name,
                            value=numeric_value,
                            currency=code if is_ad_money(metric_name) else None,
                            source=f"lingxing_ads_{report_type}",
                        ))
                mark_coverage(
                    db,
                    store_id=store_id,
                    metric_date=metric_date,
                    dimension=dimension,
                    row_count=len(rows),
                )
                db.commit()
                report_counts[report_type] += len(rows)

    return {
        "report_date": report_date if isinstance(report_date, str) else "per-store date window",
        "stores_seen": len(stores),
        "active_ad_stores": len(active_ad_stores),
        "report_days_per_store": (
            1 if isinstance(report_date, str) else "per-store date window"
        ),
        "rows": report_counts,
    }
