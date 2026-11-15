"""Notification materiality, separate from source facts and broad screening.

These are operational defaults, not profitability or Amazon compliance limits.
Currency-based floors apply only to explicitly observed USD evidence.
"""
from datetime import date, timedelta
import math

from sqlalchemy import select

from jx_alert.storage.models import DailyMetric, DataCoverage


POLICY_VERSION = 1
POLICY = {
    "version": POLICY_VERSION, "ordinary_confirmation_days": 2,
    "baseline": "完整前七日；不足时精确前一日",
    "sales_decline": "订单/销量下降30%且减少3，基线至少5；销售额下降30%且减少100美元",
    "spend_increase": "花费增加40%且多花30美元，当期至少50美元",
    "zero_conversion": "归因成熟，至少30次点击、50美元花费且零订单",
    "kpi": "CTR/CPC/CDR/CPS/ROAS/CPA/TCOAS恶化至少30%，并满足绝对幅度与样本门槛",
    "urgent": "严重恶化可单日提示；否则连续两个完整日期达到门槛",
    "missing": "缺失、无记录、授权失败、未知币种/时区不判正常，也不补零",
}
_RATIOS = {
    "CTR": ("clicks", "impressions", -1, .002),
    "CPC": ("spend", "clicks", 1, .25),
    "CDR": ("orders", "clicks", -1, .02),
    "CPS": ("spend", "units", 1, 5.),
    "ROAS": ("sales", "spend", -1, .5),
    "CPA": ("spend", "orders", 1, 5.),
    "TCOAS": ("spend", "total_sales", 1, .05),
}


def _valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def material_change(by_day, target, metric):
    """Require impact and sample size; repeated polling never counts as a day."""
    target = date.fromisoformat(target) if isinstance(target, str) else target
    if metric == "zero_conversion":
        row = by_day.get(target, {})
        return (row.get("currency") == "USD" and _valid(row.get("spend")) and row["spend"] >= 50
                and _valid(row.get("clicks")) and row["clicks"] >= 30
                and _valid(row.get("orders")) and row["orders"] == 0)
    if metric in _RATIOS:
        numerator, denominator, direction, floor = _RATIOS[metric]
        fields = {numerator, denominator}
        fields |= ({"impressions", "clicks"} if metric == "CTR" else {"clicks", "spend"})
    else:
        if metric not in {"orders", "units", "sales", "spend"}:
            return False
        fields, direction, floor = {metric}, (1 if metric == "spend" else -1), (100 if metric == "sales" else 30 if metric == "spend" else 3)
    seven = [target - timedelta(days=i) for i in range(1, 8)]

    def period(days):
        rows = [by_day.get(day, {}) for day in days]
        if not all(_valid(row.get(field)) for row in rows for field in fields):
            return None
        for row in rows:
            for field in fields & {"spend", "sales", "total_sales"}:
                if row.get("currency" if field == "spend" else field + "_currency") != "USD":
                    return None
        totals = {field: math.fsum(row[field] for row in rows) / len(rows) for field in fields}
        if metric in _RATIOS:
            if totals[denominator] <= 0:
                return None
            value = totals[numerator] / totals[denominator]
            if metric == "CTR":
                if totals["impressions"] < 2000:
                    return None
            elif totals["clicks"] < 30 or totals["spend"] < 50:
                return None
            if metric in {"CPA", "CPS"} and totals[denominator] < 3:
                return None
            if metric == "TCOAS" and totals["total_sales"] < 200:
                return None
        else:
            value = totals[metric]
        return value

    current = period([target])
    reference = period(seven)
    seven_ready = reference is not None
    if reference is None:
        reference = period([seven[0]])
    if current is None or reference is None or reference <= 0:
        return False
    if metric in {"orders", "units"} and reference < 5 or metric == "sales" and reference < 200:
        return False
    if metric == "spend" and current < 50:
        return False
    threshold = .4 if metric == "spend" else .3

    def adverse(value, base, *, severe=False):
        ratio = (.8 if metric == "spend" else .5) if severe else threshold
        absolute = floor * (2 if severe else 1)
        return (base > 0 and direction * (value - base) >= absolute
                and direction * (value - base) / base >= ratio - 1e-10)

    if not adverse(current, reference):
        return False
    if adverse(current, reference, severe=True):
        return True
    previous = period([seven[0]])
    previous_reference = reference if seven_ready else period([seven[1]])
    return (previous is not None and previous_reference is not None
            and adverse(previous, previous_reference))


def notification_evidence(db, store_id, dimension, entity_id, target):
    target = date.fromisoformat(target) if isinstance(target, str) else target
    first = target - timedelta(days=7)
    covered = {row.metric_date for row in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store_id, DataCoverage.dimension == dimension,
        DataCoverage.metric_date.between(first, target), DataCoverage.status == "COMPLETE"))}
    result = {}
    for row in db.scalars(select(DailyMetric).where(DailyMetric.store_id == store_id,
            DailyMetric.dimension == dimension, DailyMetric.entity_id == entity_id,
            DailyMetric.metric_date.in_(covered))):
        item = result.setdefault(row.metric_date, {})
        item[row.metric_name] = row.value
        if row.metric_name in {"spend", "sales"}:
            item["currency" if row.metric_name == "spend" else "sales_currency"] = row.currency
    return result


def material_finding(db, settings, store_id, dimension, entity_id, target, metric):
    if not getattr(settings, "alert_materiality_enabled", False):
        return True
    if not target:
        return False
    return material_change(notification_evidence(db, store_id, dimension, entity_id, target), target, metric)


def material_rule_notification(db, settings, anomaly):
    if not getattr(settings, "alert_materiality_enabled", False):
        return True
    if anomaly.rule == "store_status_not_normal" or anomaly.rule.endswith("_freshness"):
        return True
    if anomaly.status == "RECOVERED":
        from jx_alert.notifications.outbox import NotificationOutbox
        return db.scalar(select(NotificationOutbox.id).where(
            NotificationOutbox.idempotency_key.startswith(f"anomaly:{anomaly.dedupe_key}:"),
            NotificationOutbox.idempotency_key.contains(anomaly.first_seen.isoformat()),
            NotificationOutbox.status == "DELIVERED").limit(1)) is not None
    scope = (anomaly.evaluation_scope or "").split(":")
    if len(scope) != 3 or scope[1] not in {"sales", "campaign", "product_ad"}:
        return True  # Existing health/authorization protections are independent.
    dimension = scope[2] if scope[1] == "sales" else scope[1] + "_" + scope[2]
    observed = anomaly.metrics or {}
    target = observed.get("attribution_complete_report_date") or observed.get("report_date")
    entity = observed.get("ad_id") or anomaly.entity_id
    metric = {"orders_decrease": "orders", "units_decrease": "units", "sales_decrease": "sales",
              "spend_no_conversion": "zero_conversion", "low_spend_no_conversion": "zero_conversion",
              "high_ad_spend": "spend"}.get(anomaly.rule)
    if metric:
        return material_finding(db, settings, anomaly.store_id, dimension, entity, target, metric)
    if anomaly.rule == "campaign_over_budget" and target and observed.get("budget_type") == "daily":
        row = notification_evidence(db, anomaly.store_id, dimension, entity, target).get(date.fromisoformat(target), {})
        budget = observed.get("daily_budget")
        spend = row.get("spend")
        return (_valid(budget) and budget > 0 and _valid(spend) and row.get("currency") == "USD"
                and spend >= budget * 1.2 and spend - budget >= 30)
    return False  # Budget utilization alone and low-impact screening stay silent.
