"""Read-only company snapshot from exact, completed Lingxing daily partitions."""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from jx_alert.storage.models import DailyMetric, DataCoverage, Store

BEIJING = ZoneInfo("Asia/Shanghai")
MARKETPLACE = ZoneInfo("America/Los_Angeles")
OPENS, CLOSES = time(8, 30), time(8, 40)
AD_DIMENSIONS = ("campaign_sp", "campaign_sb", "campaign_sd")
DIMENSIONS = ("store", "sku_asin", *AD_DIMENSIONS)
MATURE_DAYS = 14  # Matches the existing store-level SP+SB+SD attribution policy.
MATURE_REPORT_LAG_DAYS = MATURE_DAYS + 1  # Allow the ordinary prior 18:30 refresh to clear the reporting lag.
REPORTING_DELAY = timedelta(hours=12)  # Amazon Ads: conversions may take 12 hours to appear.
METRICS = ("sales", "orders", "spend", "acos", "tacos", "cvr", "ppc")
LATEST = ("sales", "orders", "spend", "tacos", "ppc")


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _currency(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) is not None


def _value(value, *, count=False):
    return (type(value) in (float, int) and math.isfinite(value) and value >= 0
            and (not count or int(value) == value))


def _sum(values):
    if not values or any(value is None for value in values):
        return None
    try:
        result = math.fsum(values)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def report_dates(now):
    """Anchor all stores to the planned Beijing send, even when previewed later."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Company digest requires a timezone-aware clock")
    scheduled = datetime.combine(now.astimezone(BEIJING).date(), OPENS, BEIJING)
    latest = scheduled.astimezone(MARKETPLACE).date() - timedelta(days=1)
    return latest, latest - timedelta(days=MATURE_REPORT_LAG_DAYS)


def _partition(coverage, facts, day, now, *, lag=0):
    # A 'complete' report fetched before its day closed (or before attribution
    # matured) cannot prove the requested denominator. No future captures.
    earliest = datetime.combine(day + timedelta(days=lag + 1), time.min, MARKETPLACE)
    if lag:
        earliest = earliest.astimezone(timezone.utc) + REPORTING_DELAY
    return (coverage is not None and coverage.status == "COMPLETE"
            and type(coverage.row_count) is int
            and coverage.row_count >= 0 and coverage.completed_at is not None
            and earliest <= _utc(coverage.completed_at) <= now
            and coverage.row_count == len({fact.entity_id for fact in facts}))


def _by_entity(facts, metric):
    return {fact.entity_id: fact for fact in facts if fact.metric_name == metric}


def _store_totals(sid, day, coverage, rows, now):
    total, grain = rows.get((sid, day, "store"), []), rows.get((sid, day, "sku_asin"), [])
    if not (total and grain
            and _partition(coverage.get((sid, day, "store")), total, day, now)
            and _partition(coverage.get((sid, day, "sku_asin")), grain, day, now)
            and {row.entity_id for row in total} == {sid}
            and all(row.source == "lingxing_sales_report" for row in (*total, *grain))):
        return {}, None
    entities = {row.entity_id for row in grain}
    values, currency = {}, None
    totals = {item.metric_name: item for item in total}
    for metric in ("orders", "sales"):
        whole, parts = totals.get(metric), _by_entity(grain, metric)
        count = metric == "orders"
        if (whole is None or set(parts) != entities or not _value(whole.value, count=count)
                or not all(_value(row.value, count=count) for row in parts.values())):
            continue
        summed = _sum([row.value for row in parts.values()])
        # MariaDB legacy FLOAT independently rounds the normalized grain and
        # the stored aggregate. Reconcile within Float precision, not cents.
        if summed is None or (whole.value != summed if count else
                              not math.isclose(whole.value, summed, rel_tol=2e-7, abs_tol=1e-8)):
            continue
        if metric == "sales":
            if not _currency(whole.currency) or any(row.currency != whole.currency for row in parts.values()):
                continue
            currency = whole.currency
        values[metric] = whole.value
    return values, currency


def _ad_facts(sid, day, coverage, rows, now, *, mature=False):
    verified = {}
    for dimension in AD_DIMENSIONS:
        facts = rows.get((sid, day, dimension), [])
        if (_partition(coverage.get((sid, day, dimension)), facts, day, now,
                       lag=MATURE_DAYS if mature else 0)
                and all(row.source == "lingxing_ads_" + dimension.removeprefix("campaign_") for row in facts)):
            verified[dimension] = facts
    return verified


def _ad_totals(verified, dimensions, fields, currency):
    if not all(dimension in verified for dimension in dimensions):
        return {}
    selected = [row for dimension in dimensions for row in verified[dimension]]
    # A complete empty partition contributes no rows, but all-empty AD
    # reports do not make up explicit company spend/conversion zero.
    if not selected:
        return {}
    entities = {(row.dimension, row.entity_id) for row in selected}
    values = {}
    for metric in fields:
        matching = [row for row in selected if row.metric_name == metric]
        if ({(row.dimension, row.entity_id) for row in matching} != entities
                or not all(_value(row.value, count=metric in {"orders", "clicks"}) for row in matching)
                or metric in {"spend", "sales"} and
                (not _currency(currency) or any(row.currency != currency for row in matching))):
            continue
        values[metric] = _sum([row.value for row in matching])
    return values


def _complete(rows, family, *fields):
    return len([row for row in rows if all(row[family].get(field) is not None for field in fields)])


def _aggregate(rows):
    sums = {family: {metric: _sum([row[family].get(metric) for row in rows]) for metric in fields}
            for family, fields in {
                "business": ("sales", "orders"), "ads": ("spend", "clicks"),
                "mature": ("spend", "sales"), "mature_sp": ("orders", "clicks"),
            }.items()}
    sales, orders, spend = sums["business"]["sales"], sums["business"]["orders"], sums["ads"]["spend"]
    mature = sums["mature"]
    return {
        "values": dict(sales=sales, orders=int(orders) if orders is not None else None, spend=spend,
                       tacos=_ratio(spend, sales), ppc=_ratio(spend, sums["ads"]["clicks"]),
                       acos=_ratio(mature["spend"], mature["sales"]),
                       cvr=_ratio(sums["mature_sp"]["orders"], sums["mature_sp"]["clicks"])),
        "covered": dict(sales=_complete(rows, "business", "sales"), orders=_complete(rows, "business", "orders"),
                        spend=_complete(rows, "ads", "spend"),
                        tacos=sum(row["ads"].get("spend") is not None
                                  and row["business"].get("sales") is not None for row in rows),
                        ppc=_complete(rows, "ads", "spend", "clicks"),
                        acos=_complete(rows, "mature", "spend", "sales"),
                        cvr=_complete(rows, "mature_sp", "orders", "clicks")),
        "store_count": len(rows),
    }


def company_snapshot(db, scope, confirmed_zones, *, now):
    """Produce no partial total. Currency cohorts are exhaustive or suppressed."""
    latest, mature = report_dates(now)
    if not scope:
        raise ValueError("Company digest requires an explicit nonempty store scope")
    scope = sorted(set(scope))
    stores = {store.sid: store for store in db.scalars(select(Store).where(Store.sid.in_(scope)))}
    # A single bounded date/dimension predicate per table; no provider or collection side effects.
    def predicates(model):
        return (model.store_id.in_(scope), model.metric_date.in_((latest, mature)),
                model.dimension.in_(DIMENSIONS))

    coverage = {(row.store_id, row.metric_date, row.dimension): row for row in db.scalars(
        select(DataCoverage).where(*predicates(DataCoverage)))}
    facts = defaultdict(list)
    for row in db.scalars(select(DailyMetric).where(*predicates(DailyMetric))):
        facts[(row.store_id, row.metric_date, row.dimension)].append(row)

    stores_missing_currency = 0
    cohorts, rows = defaultdict(list), []
    for sid in scope:
        store = stores.get(sid)
        row = dict(business={}, ads={}, mature={}, mature_sp={})
        rows.append(row)
        if (store is None or store.status != 1
                or store.timezone != MARKETPLACE.key or confirmed_zones.get(sid) != MARKETPLACE.key):
            stores_missing_currency += 1
            continue
        row["business"], currency = _store_totals(sid, latest, coverage, facts, now)
        ads = _ad_facts(sid, latest, coverage, facts, now)
        attributed = _ad_facts(sid, mature, coverage, facts, now, mature=True)
        row["ads"] = _ad_totals(ads, AD_DIMENSIONS, ("spend", "clicks"), currency)
        row["mature"] = _ad_totals(attributed, AD_DIMENSIONS, ("spend", "sales"), currency)
        # Standard SB/SD purchases can include views under vCPM. Only SP
        # click-attributed orders can prove a click conversion rate here.
        row["mature_sp"] = _ad_totals(attributed, ("campaign_sp",), ("orders", "clicks"), None)
        if currency:
            cohorts[currency].append(row)
        else:
            stores_missing_currency += 1
    orders = _sum([row["business"].get("orders") for row in rows])
    return {
        "date": latest.isoformat(), "mature_date": mature.isoformat(),
        "store_count": len(scope), "unconfirmed_currency_stores": stores_missing_currency,
        "company_orders": int(orders) if orders is not None else None,
        "orders_covered": _complete(rows, "business", "orders"),
        "currencies": ({code: _aggregate(group) for code, group in sorted(cohorts.items())}
                       if not stores_missing_currency else {}),
    }
