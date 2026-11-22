"""Exact-date operations brief from explicit Lingxing source partitions."""

from datetime import date, datetime, time, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.lingxing.api_business import FINANCE, PRODUCT, api_business_context
from jx_alert.storage.models import DataCoverage

_FORWARD_WINDOW = timedelta(minutes=90)
_PRODUCT_AD_FORMATS = ["SP", "SB", "SBV", "SD", "共享分摊"]


def _currency(packet):
    value = packet.get("currency")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) else None


def _quantity(value):
    return int(value) if (type(value) in (int, float) and math.isfinite(value)
                          and value >= 0 and int(value) == value) else None


def _money(value, *, deduction=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return abs(value) if deduction else value


def _valid_partition(coverage, *, day, clock, zone):
    if not coverage or coverage.status != "COMPLETE" or not coverage.row_count:
        return False
    captured = coverage.completed_at
    if not isinstance(captured, datetime):
        return False
    captured = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured
    local_day = clock.astimezone(zone).date()
    if day > local_day or captured > clock:
        return False
    if day == local_day:
        return captured.astimezone(zone).date() == day and clock - captured <= _FORWARD_WINDOW
    return captured >= datetime.combine(day + timedelta(days=1), time.min, zone)


def owner_daily_rows(db, stores, report_date, *, now=None):
    """Build rows for one exact report date, without fallback or source substitution."""
    if type(report_date) is not date:
        raise ValueError("Owner report requires an explicit date")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Owner report requires a timezone-aware clock")
    scope = {str(store.sid): store for store in stores if store.status == 1}
    parts = {(c.store_id, c.dimension): c for c in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id.in_(scope), DataCoverage.metric_date == report_date,
        DataCoverage.dimension.in_((PRODUCT, FINANCE)),
    ))} if scope else {}
    rows, provisional = [], False
    for sid, store in scope.items():
        row = {"store_id": sid, "store_name": store.name, "currency": None,
               "ad_spend": None, "covered_ad_types": [], "sales": None,
               "return_units": None, "sold_units": None, "refund_amount": None}
        rows.append(row)
        try:
            zone = ZoneInfo(store.timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            continue
        local_day = clock.astimezone(zone).date()
        provisional |= report_date >= local_day
        product_ok = _valid_partition(parts.get((sid, PRODUCT)), day=report_date, clock=clock, zone=zone)
        finance_ok = _valid_partition(parts.get((sid, FINANCE)), day=report_date, clock=clock, zone=zone)
        if not product_ok and not finance_ok:
            continue
        payload = api_business_context(db, sid, report_date)
        product = payload["product"] if product_ok else {}
        finance = payload["settlement"] if finance_ok else {}
        product_money, finance_money = _currency(product), _currency(finance)
        row["currency"] = product_money or finance_money
        product_metrics, finance_metrics = product.get("metrics", {}), finance.get("metrics", {})
        if product_money:
            raw_spend = _money(product_metrics.get("adSpend"))
            if raw_spend is not None and raw_spend >= 0:
                row.update(ad_spend=raw_spend, covered_ad_types=list(_PRODUCT_AD_FORMATS))
            row["sales"] = _money(product_metrics.get("netAmount"))
        row["return_units"] = _quantity(product_metrics.get("returnGoodsCount"))
        row["sold_units"] = _quantity(product_metrics.get("volume"))
        if finance_money and finance_money == row["currency"]:
            row["refund_amount"] = _money(finance_metrics.get("totalSalesRefunds"), deduction=True)
    return rows, provisional

def owner_daily_delivery_card(settings, key, payload):
    """A frozen aggregate must not migrate to a different group or owner set."""
    from jx_alert.notifications.cards import with_mentions
    from jx_alert.notifications.routing import (
        ApplicationRoutingError, _load_routing, report_group_delivery, report_group_owner_mentions,
    )
    from jx_alert.notifications.store_health import card_fits

    if (not getattr(settings, "feishu_owner_daily_report_enabled", False)
            or payload.get("transport") != "app" or payload.get("fixed_route") is not True
            or not key.startswith("scheduled-daily:morning:") or not payload.get("store_ids")):
        return None
    try:
        routing = _load_routing(settings.feishu_routing_file)
        current = report_group_delivery(settings, routing, role="daily_snapshot")
    except (OSError, ValueError, RuntimeError, TypeError, ApplicationRoutingError):
        return None
    original = payload.get("card")
    if current != payload.get("delivery") or not isinstance(original, dict):
        return None
    mentions = list(dict.fromkeys(uid for sid in payload["store_ids"] for uid in
        report_group_owner_mentions(routing, current, sid)))
    value = with_mentions(original, mentions)
    return value if card_fits(value) else original
