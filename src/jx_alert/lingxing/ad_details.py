"""Read-only SB/SD detail partitions, separate from authoritative activity totals."""
from datetime import date, timedelta
import hashlib
import json
import math
import re

from sqlalchemy import delete, select

from jx_alert.lingxing.ad_currency import advertising_currency, is_ad_money
from jx_alert.lingxing.client import LingxingError
from jx_alert.lingxing.report_dates import normalize_store_report_dates
from jx_alert.lingxing.report_metrics import AD_REPORT_METRICS
from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import Campaign, DataCoverage, DailyMetric, RawSnapshot


DETAIL_LABELS = {
    "creative_sb": "品牌推广创意",
    "target_sb": "品牌推广投放",
    "purchased_asin_sb": "品牌推广归因购买",
    "target_sd": "展示推广商品定位",
    "purchased_asin_sd": "展示推广已购商品",
}

_BRAND_PURCHASE = {
    "sales14d": "sales_14d", "orders14d": "orders_14d", "units_sold14d": "units_14d",
    "new_to_brand_sales14d": "new_to_brand_sales_14d",
    "new_to_brand_purchases14d": "new_to_brand_orders_14d",
    "new_to_brand_units_sold14d": "new_to_brand_units_14d",
    "new_to_brand_sales_percentage14d": "new_to_brand_sales_percentage_14d",
    "new_to_brand_purchases_percentage14d": "new_to_brand_orders_percentage_14d",
    "new_to_brand_units_sold_percentage14d": "new_to_brand_units_percentage_14d",
    "units": "units", "same_units": "same_units",
}
_DISPLAY_PURCHASE = {f"other_{metric}{window}": f"other_{metric}{window}"
                     for metric in ("sales", "orders", "units") for window in ("", "_1d", "_7d", "_14d", "_30d")}


def daily_detail_dates(ad_report_dates):
    """Refresh the latest completed day and its 14-day attribution checkpoint."""
    dates = {}
    for sid, window in ad_report_dates.items():
        last = date.fromisoformat(window[-1])
        dates[sid] = [(last - timedelta(days=14)).isoformat(), last.isoformat()]
    return dates


def _identifier(row, field, *, optional=False):
    value = row.get(field)
    if optional and value in (None, "", 0, "0") and not isinstance(value, bool):
        return None
    if type(value) not in (int, str) or not re.fullmatch(r"[1-9][0-9]{0,39}", str(value)):
        raise ValueError(f"Invalid detail identifier: {field}")
    return str(value)


def _text_key(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid detail identifier: {field}")
    return value


def _entity(row, dimension):
    legacy = dimension == "purchased_asin_sb"
    profile = _identifier(row, "profile_id")
    campaign = _identifier(row, "campaign_id", optional=legacy)
    group = _identifier(row, "ad_group_id", optional=legacy)
    key = {"profile": profile, "campaign": campaign, "group": group}
    if dimension == "creative_sb":
        key["creative"] = _identifier(row, "ad_creative_id")
    elif dimension == "target_sb":
        keyword = _identifier(row, "keyword_id", optional=True)
        target = _identifier(row, "target_id", optional=True)
        if bool(keyword) == bool(target):
            raise ValueError("SB targeting row requires one keyword or target ID")
        key["keyword" if keyword else "target"] = keyword or target
    elif dimension == "target_sd":
        key["target"] = _identifier(row, "target_id")
    elif legacy:
        key["asin"] = _text_key(row, "asin")
        key["attribution"] = _text_key(row, "attribution_type")
        if not campaign:
            key["campaign_name"] = _text_key(row, "campaign_name")
        if not group and isinstance(row.get("ad_group_name"), str) and row["ad_group_name"]:
            key["group_name"] = row["ad_group_name"]
    else:
        key.update({f: _text_key(row, f) for f in ("tactic", "asin", "sku", "other_asin")})
    identity = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    if len(identity) > 200:
        identity = "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
    return identity, campaign


def _numeric_metrics(row, dimension):
    if dimension == "purchased_asin_sb":
        names, required = _BRAND_PURCHASE, {"sales14d", "orders14d", "units_sold14d"}
    elif dimension == "purchased_asin_sd":
        names, required = _DISPLAY_PURCHASE, {"other_sales", "other_units"}
    else:
        names, required = AD_REPORT_METRICS, {"cost", "clicks", "impressions", "orders", "sales"}
    if any(row.get(name) is None for name in required):
        raise ValueError("Detail row is missing required metrics")
    result = {}
    for field, metric in names.items():
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, bool):
            raise ValueError("Invalid advertising detail metric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Invalid advertising detail metric")
        result[metric] = number
    return result


def _save_partition(db, sid, day, dimension, rows, currencies, profiles):
    source = f"lingxing_{dimension}"
    normalized = []
    seen = set()
    for row in rows:
        if row.get("report_date") != day.isoformat():
            raise ValueError("Detail report returned a different report date")
        identity, campaign = _entity(row, dimension)
        if identity in seen:
            raise ValueError("Advertising detail report contains duplicate entities")
        seen.add(identity)
        values = _numeric_metrics(row, dimension)
        account = advertising_currency(currencies, sid, row,
                                       campaign_profile=profiles.get((dimension[-2:], campaign)))
        reported = row.get("currency") if dimension == "purchased_asin_sd" else None
        if reported is not None:
            valid = isinstance(reported, str) and re.fullmatch(r"[A-Z]{3}", reported)
            currency = reported if valid and (account is None or account == reported) else None
        else:
            currency = account
        normalized.append((identity, values, currency))
    db.execute(delete(DailyMetric).where(DailyMetric.store_id == sid,
               DailyMetric.metric_date == day, DailyMetric.dimension == dimension))
    db.add(RawSnapshot(source=source, store_id=sid, payload={"report_date": day.isoformat(),
                       "rows": rows, "entity_ids": [item[0] for item in normalized]}))
    for identity, values, currency in normalized:
        db.add_all(DailyMetric(store_id=sid, metric_date=day, dimension=dimension,
                              entity_id=identity, metric_name=metric, value=value, source=source,
                              currency=currency if is_ad_money(metric) and "percentage" not in metric else None)
                   for metric, value in values.items())
    mark_coverage(db, store_id=sid, metric_date=day, dimension=dimension, row_count=len(rows))
    db.commit()


def collect_ad_details(client, db, report_dates, *, stores_response, ad_currencies=None):
    """Persist each validated endpoint independently and retain prior facts on failure."""
    counts = dict.fromkeys(DETAIL_LABELS, 0)
    failures = []
    for store in stores_response["data"]:
        sid = str(store["sid"])
        if int(store.get("status") or 0) != 1 or int(store.get("has_ads_setting") or 0) != 1:
            continue
        if isinstance(report_dates, dict) and sid not in report_dates:
            continue
        profiles = {(r.campaign_type, r.campaign_id): r.profile_id for r in db.scalars(
            select(Campaign).where(Campaign.store_id == sid, Campaign.campaign_type.in_(["sb", "sd"])))}
        for report_date in normalize_store_report_dates(report_dates, sid):
            day = date.fromisoformat(report_date)
            for dimension in DETAIL_LABELS:
                payload = {"sid": store["sid"], "report_date": day.isoformat()}
                if dimension == "purchased_asin_sd":
                    payload["show_detail"] = 1
                try:
                    rows = list(client.iter_advertising_detail_report(dimension, payload))
                    _save_partition(db, sid, day, dimension, rows, ad_currencies or {}, profiles)
                    counts[dimension] += len(rows)
                except (LingxingError, ValueError, TypeError) as exc:
                    db.rollback()
                    old = db.scalar(select(DataCoverage).where(DataCoverage.store_id == sid,
                                    DataCoverage.metric_date == day, DataCoverage.dimension == dimension))
                    coverage = mark_coverage(db, store_id=sid, metric_date=day, dimension=dimension,
                                             row_count=old.row_count if old else 0)
                    coverage.status = "FAILED"
                    db.commit()
                    failures.append({"store_id": sid, "date": report_date, "dimension": dimension,
                                     "error_type": type(exc).__name__})
    return {"status": "PARTIAL_FAILED" if failures else "COMPLETE", "rows": counts, "failures": failures}
