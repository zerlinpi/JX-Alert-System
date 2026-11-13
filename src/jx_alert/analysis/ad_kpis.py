"""Advertising ratios from complete, currency-matched source observations."""
from datetime import date, timedelta
import hashlib
import json
import math
import re

from sqlalchemy import select

from jx_alert.storage.models import DailyMetric, DataCoverage


KPI_SPECS = {
    "CTR": ("点击率（CTR）", "clicks", "impressions", "percent", "down", "点击率下降", False),
    "CPC": ("点击花费（CPC）", "spend", "clicks", "money", "up", "点击花费上升", False),
    "CDR": ("点击转化率（CDR）", "orders", "clicks", "percent", "down", "点击转化率下降", True),
    "CPS": ("单件转化花费（CPS）", "spend", "units", "money", "up", "单件转化花费上升", True),
    "ROAS": ("广告投入产出（ROAS）", "sales", "spend", "multiple", "down", "广告投入产出下降", True),
    "CPA": ("单均花费（CPA）", "spend", "orders", "money", "up", "广告单均花费上升", True),
    "TCOAS": ("总销售广告费占比（TCOAS）", "spend", "total_sales", "percent", "up", "总销售广告费占比上升", False),
}
KPI_METRICS = {"spend", "sales", "orders", "units", "clicks", "impressions", "total_sales"}


def _valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _code(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value)


def _period(records, days, numerator, denominator):
    selected = [records.get(day, {}) for day in days]
    if not all(_valid(row.get(key)) for row in selected for key in (numerator, denominator)):
        return None, "INSUFFICIENT_DATA", None
    monetary = {numerator, denominator} & {"spend", "sales", "total_sales"}
    codes = {row.get("currency" if metric == "spend" else metric + "_currency")
             for row in selected for metric in monetary}
    if monetary and (len(codes) != 1 or any(not _code(code) for code in codes)):
        return None, "CURRENCY_UNCONFIRMED", None
    currency = next(iter(codes)) if codes else None
    divisor = math.fsum(row[denominator] for row in selected)
    if divisor <= 0:
        return None, "ZERO_DENOMINATOR", currency
    value = math.fsum(row[numerator] for row in selected) / divisor
    return (value, "VALID", currency) if math.isfinite(value) else (None, "INSUFFICIENT_DATA", currency)


def compare_kpis(by_day, target, *, min_clicks=10, ratio=0.15, maturity_days=0,
                 min_impressions=1000, min_spend=20, store_scope=False):
    """Use a ratio of seven-day sums; fall back to the exact previous day."""
    if not 0 < ratio < 1:
        raise ValueError("KPI change ratio must be between zero and one")
    target = date.fromisoformat(target) if isinstance(target, str) else target
    results = {}
    for code, (label, numerator, denominator, display, direction, title, conversion) in KPI_SPECS.items():
        on = target - timedelta(days=maturity_days if conversion else 0)
        entry = {"label": label, "format": display, "current_date": on.isoformat(),
                 "formula": numerator + "/" + denominator, "title": title, "direction": direction,
                 "current": None, "baseline": None, "currency": None, "change_ratio": None,
                 "status": "INSUFFICIENT_DATA", "comparison_basis": "INSUFFICIENT_DATA",
                 "comparison_dates": [], "threshold": ratio, "attribution_mature": conversion}
        results[code] = entry
        if code == "TCOAS" and not store_scope:
            entry["status"] = "SCOPE_UNSUPPORTED"
            continue
        current, status, currency = _period(by_day, [on], numerator, denominator)
        entry.update(current=current, currency=currency, status=status)
        if current is None:
            continue
        seven = [on - timedelta(days=i) for i in range(1, 8)]
        # A complete zero-denominator period remains undefined.
        baseline, prior_status, prior_currency = _period(by_day, seven, numerator, denominator)
        basis, days = "SEVEN_DAY", seven
        if prior_status in {"INSUFFICIENT_DATA", "CURRENCY_UNCONFIRMED"}:
            days = [on - timedelta(days=1)]
            basis = "PREVIOUS_DAY"
            baseline, prior_status, prior_currency = _period(by_day, days, numerator, denominator)
        entry["baseline"] = baseline
        if prior_status != "VALID":
            entry["status"] = "NO_BASELINE" if prior_status == "INSUFFICIENT_DATA" else prior_status
            continue
        if currency != prior_currency:
            entry.update(status="CURRENCY_UNCONFIRMED", baseline=None)
            continue
        entry.update(comparison_basis=basis, comparison_dates=[on.isoformat(), *[d.isoformat() for d in days]])
        required = ("impressions", min_impressions) if code == "CTR" else (
            ("spend", min_spend) if code == "TCOAS" else ("clicks", min_clicks))
        field, minimum = required
        samples = [by_day.get(d, {}).get(field) for d in [on, *days]]
        if (not all(_valid(value) for value in samples) or samples[0] < minimum
                or math.fsum(samples[1:]) / len(days) < minimum):
            entry["status"] = "LOW_SAMPLE"
            continue
        if baseline == 0:
            entry["status"] = "NO_BASELINE" if current != 0 else "NON_ADVERSE"
            continue
        change = (current - baseline) / baseline
        entry.update(change_ratio=change,
            status="ADVERSE" if (change <= -ratio if direction == "down" else change >= ratio) else "NON_ADVERSE")
    return results


def _observations(db, store_id, target, dimension, entity_id):
    first = target - timedelta(days=21)
    dimensions = [dimension] if dimension != "store" else ["store", "campaign_sp", "campaign_sb", "campaign_sd"]
    query = select(DailyMetric).where(DailyMetric.store_id == str(store_id),
        DailyMetric.metric_date.between(first, target), DailyMetric.dimension.in_(dimensions),
        DailyMetric.metric_name.in_(KPI_METRICS))
    if dimension != "store":
        if not entity_id:
            raise ValueError("Campaign and product KPI analysis requires one entity_id")
        query = query.where(DailyMetric.entity_id == str(entity_id))
    rows = db.scalars(query.order_by(DailyMetric.id)).all()
    coverage = db.scalars(select(DataCoverage).where(DataCoverage.store_id == str(store_id),
        DataCoverage.metric_date.between(first, target), DataCoverage.dimension.in_(dimensions))
        .order_by(DataCoverage.id)).all()
    complete = {(r.metric_date, r.dimension) for r in coverage if r.status == "COMPLETE"}
    if dimension == "store":
        for record in coverage:
            if record.dimension == "store":
                continue
            entities = {r.entity_id for r in rows if r.metric_date == record.metric_date
                        and r.dimension == record.dimension}
            if record.row_count > len(entities):
                complete.discard((record.metric_date, record.dimension))
    observed = {}
    ids = {}
    for offset in range(22):
        day = target - timedelta(days=offset)
        daily = [r for r in rows if r.metric_date == day]
        ad_dimensions = dimensions[1:] if dimension == "store" else dimensions
        ad_complete = all((day, dim) in complete for dim in ad_dimensions)
        values = {}
        source_ids = []
        if ad_complete:
            for metric in KPI_METRICS - {"total_sales"}:
                relevant = [r for r in daily if r.dimension in ad_dimensions and r.metric_name == metric]
                # Each report entity must have this exact metric. Empty reports
                # contribute no spend, while missing entity fields stay unknown.
                entities = {(r.dimension, r.entity_id) for r in daily if r.dimension in ad_dimensions}
                if relevant and {(r.dimension, r.entity_id) for r in relevant} == entities:
                    values[metric] = math.fsum(r.value for r in relevant) if all(_valid(r.value) for r in relevant) else None
                    source_ids.extend(r.id for r in relevant)
                    if metric in {"spend", "sales"}:
                        codes = {r.currency for r in relevant}
                        values["currency" if metric == "spend" else "sales_currency"] = next(iter(codes)) if len(codes) == 1 else None
        if dimension == "store" and (day, "store") in complete:
            total = [r for r in daily if r.dimension == "store" and r.entity_id == str(store_id) and r.metric_name == "sales"]
            if len(total) == 1:
                values["total_sales"] = total[0].value
                values["total_sales_currency"] = total[0].currency
                source_ids.append(total[0].id)
        observed[day] = values
        ids[day.isoformat()] = source_ids
    return observed, rows, coverage, ids


def advertising_kpis(db, settings, store_id, target, *, dimension="store", entity_id=None):
    """Return frozen ratios and fingerprints; never change source observations."""
    if dimension not in {"store", "campaign_sp", "campaign_sb", "campaign_sd", "product_ad_sp", "product_ad_sd"}:
        raise ValueError("Unsupported advertising KPI dimension")
    target = date.fromisoformat(target) if isinstance(target, str) else target
    observed, rows, coverage, ids = _observations(db, store_id, target, dimension, entity_id)
    lag = 14 if dimension == "store" or dimension.endswith(("_sb", "_sd")) else 7
    comparisons = compare_kpis(observed, target, min_clicks=getattr(settings, "alert_min_clicks", 10),
        ratio=getattr(settings, "alert_kpi_change_ratio", 0.15), maturity_days=lag,
        min_impressions=getattr(settings, "alert_kpi_min_impressions", 1000),
        min_spend=getattr(settings, "alert_min_spend", 20), store_scope=dimension == "store")
    for entry in comparisons.values():
        dates = set(entry["comparison_dates"] or [entry["current_date"]])
        selected = [r for r in rows if r.metric_date.isoformat() in dates]
        selected_coverage = [r for r in coverage if r.metric_date.isoformat() in dates]
        evidence = [(r.id, r.dimension, r.entity_id, r.metric_name, str(r.value), r.currency) for r in selected]
        evidence.extend((r.id, r.metric_date.isoformat(), r.dimension, r.status, r.row_count) for r in selected_coverage)
        entry["fingerprint"] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        entry["evidence_count"] = sum(len(ids[day]) for day in dates)
        entry["scope"] = "store_all_ad_types" if dimension == "store" else dimension
    return comparisons
