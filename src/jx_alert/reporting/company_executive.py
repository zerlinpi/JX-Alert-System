"""Read-only company snapshot from exact, completed Lingxing daily partitions.

Totals only contain stores whose partitions prove the metric. Stores without
that evidence are listed with a reason and are never counted as zero.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from jx_alert.notifications.operators import UNMAPPED_OPERATOR, UNNAMED_OPERATOR
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
_AD_LABELS = {"campaign_sp": "SP", "campaign_sb": "SB", "campaign_sd": "SD"}
_STATUS_ISSUES = {0: "领星停止同步", 2: "领星授权异常", 3: "领星欠费停服"}


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


def _metric_sum(rows, family, field):
    values = [row[family][field] for row in rows if row[family].get(field) is not None]
    return (_sum(values) if values else None), len(values)


def _pair_ratio(rows, numerator, denominator):
    """Weight a ratio over stores proving both sides; never mix partial sides."""
    (top_family, top), (bottom_family, bottom) = numerator, denominator
    pairs = [(row[top_family][top], row[bottom_family][bottom]) for row in rows
             if row[top_family].get(top) is not None and row[bottom_family].get(bottom) is not None]
    if not pairs:
        return None, 0
    return _ratio(_sum([a for a, _ in pairs]), _sum([b for _, b in pairs])), len(pairs)


def _aggregate(rows):
    sales, sales_n = _metric_sum(rows, "business", "sales")
    orders, orders_n = _metric_sum(rows, "business", "orders")
    spend, spend_n = _metric_sum(rows, "ads", "spend")
    tacos, tacos_n = _pair_ratio(rows, ("ads", "spend"), ("business", "sales"))
    ppc, ppc_n = _pair_ratio(rows, ("ads", "spend"), ("ads", "clicks"))
    acos, acos_n = _pair_ratio(rows, ("mature", "spend"), ("mature", "sales"))
    # Standard SB/SD purchases can include views under vCPM. Only SP
    # click-attributed orders can prove a click conversion rate here.
    cvr, cvr_n = _pair_ratio(rows, ("mature_sp", "orders"), ("mature_sp", "clicks"))
    return {
        "values": dict(sales=sales, orders=int(orders) if orders is not None else None, spend=spend,
                       tacos=tacos, ppc=ppc, acos=acos, cvr=cvr),
        "covered": dict(sales=sales_n, orders=orders_n, spend=spend_n, tacos=tacos_n, ppc=ppc_n,
                        acos=acos_n, cvr=cvr_n),
        "store_count": len(rows),
    }


def _gaps(row, latest_ads, mature_ads):
    gaps = []
    business = row["business"]
    if business.get("sales") is None or business.get("orders") is None:
        gaps.append("销售/订单日报不完整")
    missing = [_AD_LABELS[d] for d in AD_DIMENSIONS if d not in latest_ads]
    if missing:
        gaps.append("缺少当日 " + "/".join(missing) + " 广告日报")
    elif row["ads"].get("spend") is None:
        gaps.append("当日广告日报无可用记录")
    missing = [_AD_LABELS[d] for d in AD_DIMENSIONS if d not in mature_ads]
    if missing:
        gaps.append("成熟归因日 " + "/".join(missing) + " 未达成熟或缺失")
    elif row["mature"].get("spend") is None or row["mature"].get("sales") is None:
        gaps.append("成熟归因日广告无可用记录")
    return gaps


def company_snapshot(db, scope, confirmed_zones, *, now, operators=None, excluded_store_count=0):
    """Totals cover only proven stores; every omitted store is listed, never zeroed."""
    latest, mature = report_dates(now)
    if not scope:
        raise ValueError("Company digest requires an explicit nonempty store scope")
    scope = sorted(set(scope), key=lambda sid: (len(sid), sid))
    operators = operators or {}
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

    rows, issues, gaps = [], [], []
    for sid in scope:
        store = stores.get(sid)
        name = (store.name if store is not None and isinstance(store.name, str) and store.name.strip()
                else sid)
        row = dict(sid=sid, name=name, operator=operators.get(sid) or UNMAPPED_OPERATOR, currency=None,
                   business={}, ads={}, mature={}, mature_sp={})
        rows.append(row)
        issue = None
        if store is None:
            issue = "尚未同步店铺资料"
        elif store.timezone != MARKETPLACE.key or confirmed_zones.get(sid) != MARKETPLACE.key:
            issue = "报表时区未确认为美西"
        elif store.status != 1:
            issue = _STATUS_ISSUES.get(store.status, "领星店铺状态异常")
        if issue is None:
            row["business"], currency = _store_totals(sid, latest, coverage, facts, now)
            if currency is None:
                issue = "缺少完整销售日报或币种待确认"
            else:
                row["currency"] = currency
                ads = _ad_facts(sid, latest, coverage, facts, now)
                attributed = _ad_facts(sid, mature, coverage, facts, now, mature=True)
                row["ads"] = _ad_totals(ads, AD_DIMENSIONS, ("spend", "clicks"), currency)
                row["mature"] = _ad_totals(attributed, AD_DIMENSIONS, ("spend", "sales"), currency)
                row["mature_sp"] = _ad_totals(attributed, ("campaign_sp",), ("orders", "clicks"), None)
                found = _gaps(row, ads, attributed)
                if found:
                    gaps.append({"sid": sid, "name": name, "operator": row["operator"], "gaps": found})
        if issue is not None:
            issues.append({"sid": sid, "name": name, "operator": row["operator"], "issue": issue})

    cohorts = defaultdict(list)
    for row in rows:
        if row["currency"]:
            cohorts[row["currency"]].append(row)
    by_operator = defaultdict(list)
    for row in rows:
        by_operator[row["operator"]].append(row)
    operator_rows = []
    placeholders = {UNNAMED_OPERATOR: 1, UNMAPPED_OPERATOR: 2}
    for label in sorted(by_operator, key=lambda value: (placeholders.get(value, 0), value)):
        members = by_operator[label]
        codes = sorted({row["currency"] for row in members if row["currency"]})
        for code in codes or [None]:
            subset = [row for row in members if row["currency"] == code] if code else []
            operator_rows.append({"operator": label, "currency": code, "store_total": len(members),
                                  **_aggregate(subset)})
    orders, orders_n = _metric_sum(rows, "business", "orders")
    return {
        "date": latest.isoformat(), "mature_date": mature.isoformat(),
        "store_count": len(scope), "excluded_store_count": excluded_store_count,
        "unconfirmed_currency_stores": len(issues),
        "company_orders": int(orders) if orders is not None else None,
        "orders_covered": orders_n,
        "currencies": {code: _aggregate(group) for code, group in sorted(cohorts.items())},
        "operators": operator_rows,
        "issues": issues, "gaps": gaps,
    }
