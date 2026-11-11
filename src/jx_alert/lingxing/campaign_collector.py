"""Read-only SP/SB/SD campaign master-data synchronization."""
from datetime import datetime, timezone

from sqlalchemy import select

from jx_alert.lingxing.sales_collector import _upsert_store
from jx_alert.storage.models import Campaign, RawSnapshot


def _float_or_none(value, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Campaign field {field} is not numeric") from exc


def collect_campaigns(client, db, *, stores_response: dict | None = None,
                      page_size: int = 100) -> dict:
    stores_response = stores_response or client.get_stores()
    stores = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(stores, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")

    active_ad_stores = []
    for record in stores:
        store_id = _upsert_store(db, record)
        if int(record.get("status") or 0) == 1 and int(record.get("has_ads_setting") or 0) == 1:
            active_ad_stores.append((store_id, record))
    db.flush()

    total_campaigns = {"sp": 0, "sb": 0, "sd": 0}
    for store_id, store in active_ad_stores:
        for campaign_type in ("sp", "sb", "sd"):
            rows = list(client.iter_advertising_campaigns(
                campaign_type,
                {"sid": store["sid"]},
                length=page_size,
            ))
            db.add(RawSnapshot(
                source=f"lingxing_{campaign_type}_campaigns",
                store_id=store_id,
                payload={"rows": rows},
            ))

            seen_ids: set[str] = set()
            for record in rows:
                campaign_id = record.get("campaign_id")
                if campaign_id is None:
                    raise RuntimeError(
                        f"{campaign_type.upper()} campaign record is missing campaign_id"
                    )
                campaign_id = str(campaign_id)
                if campaign_id in seen_ids:
                    raise RuntimeError(
                        f"{campaign_type.upper()} campaign list contains duplicate campaign_id"
                    )
                seen_ids.add(campaign_id)

                row = db.scalar(select(Campaign).where(
                    Campaign.store_id == store_id,
                    Campaign.campaign_type == campaign_type,
                    Campaign.campaign_id == campaign_id,
                ))
                if row is None:
                    row = Campaign(
                        store_id=store_id,
                        campaign_type=campaign_type,
                        campaign_id=campaign_id,
                    )
                    db.add(row)
                row.profile_id = (
                    str(record["profile_id"]) if record.get("profile_id") is not None else None
                )
                row.name = record.get("name")
                row.state = record.get("state")
                row.serving_status = record.get("serving_status")
                row.budget = _float_or_none(record.get("budget"), "budget")
                row.budget_type = record.get("budget_type")
                row.start_date = record.get("start_date")
                row.end_date = record.get("end_date")
                row.last_over_budget_at = (
                    record.get("lastOverBudgetAt") or record.get("last_over_budget_at")
                )
                row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

            db.commit()
            total_campaigns[campaign_type] += len(rows)

    return {
        "stores_seen": len(stores),
        "active_ad_stores": len(active_ad_stores),
        "campaigns": total_campaigns,
    }
