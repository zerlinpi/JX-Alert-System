"""Compact business reports from the official business dashboard, never renamed sales."""
from datetime import date, timedelta
from decimal import Decimal
import math
import re

from sqlalchemy import select

from jx_alert.notifications.cards import card
from jx_alert.storage.models import DailyMetric, DataCoverage


DIMENSION = "business_dashboard"
SOURCE = "lingxing_business_dashboard"
# Source ratios are fractions. Expenses/refunds are signed deductions upstream;
# the operator report shows their magnitude, without altering stored observations.
BUSINESS_METRICS = (
    ("volume", "销量", "count"),
    ("netAmount", "净销售额", "money"),
    ("platformIncome", "平台收入", "money"),
    ("grossProfit", "毛利润", "money"),
    ("grossRate", "毛利率", "rate"),
    ("totalAdsCostRate", "广告费占比¹", "expense_rate"),
    ("totalRefundsRate", "退款率¹", "expense_rate"),
)


def business_daily_context(db, sid, report_date):
    day = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    previous = day - timedelta(days=1)
    coverage = {r.metric_date: r for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == str(sid), DataCoverage.dimension == DIMENSION,
        DataCoverage.metric_date.in_([day, previous])))}
    facts = list(db.scalars(select(DailyMetric).where(DailyMetric.store_id == str(sid),
        DailyMetric.dimension == DIMENSION, DailyMetric.entity_id == str(sid),
        DailyMetric.source == SOURCE, DailyMetric.metric_date.in_([day, previous]))))
    values = {}
    currencies = {}
    for row in facts:
        part = coverage.get(row.metric_date)
        if (part is not None and part.status == "COMPLETE" and part.row_count > 0
                and isinstance(row.value, (int, float)) and math.isfinite(row.value)):
            values[row.metric_date, row.metric_name] = row.value
            currencies[row.metric_date, row.metric_name] = row.currency
    metrics, missing = [], []
    for key, label, unit in BUSINESS_METRICS:
        current, prior = values.get((day, key)), values.get((previous, key))
        current_currency, prior_currency = currencies.get((day, key)), currencies.get((previous, key))
        if unit == "money":
            if not isinstance(current_currency, str) or not re.fullmatch(r"[A-Z]{3}", current_currency):
                current = None
            if not isinstance(prior_currency, str) or not re.fullmatch(r"[A-Z]{3}", prior_currency):
                prior = None
        if unit == "expense_rate":
            current = abs(current) if current is not None else None
            prior = abs(prior) if prior is not None else None
        comparable = not (unit == "money" and current is not None and prior is not None
                          and current_currency != prior_currency)
        delta = None
        if current is not None and prior is not None and comparable:
            a, b = Decimal(str(current)), Decimal(str(prior))
            delta = float((a - b) if unit.endswith("rate") else (a - b) / abs(b)) if (
                unit.endswith("rate") or b != 0) else (0.0 if a == 0 else None)
        if current is None:
            missing.append(label)
        metrics.append({"key": key, "label": label, "current": current, "previous": prior,
            "unit": unit, "change": delta, "comparable": comparable,
            "currency": current_currency if unit == "money" else None,
            "previous_currency": prior_currency if unit == "money" else None})
    money_currencies = {m["currency"] for m in metrics if m["unit"] == "money" and m["current"] is not None}
    current_coverage = coverage.get(day)
    availability = ("FAILED" if current_coverage is not None and current_coverage.status == "FAILED" else
                    "AVAILABLE" if any(m["current"] is not None for m in metrics) else "MISSING")
    return {"report_date": day.isoformat(), "previous_date": previous.isoformat(),
        "currency": next(iter(money_currencies)) if len(money_currencies) == 1 else None,
        "basis": "领星经营看板", "metrics": metrics, "missing_metrics": missing,
        "availability": availability}


def business_display(value, unit, currency=None):
    if value is None:
        return "—"
    if unit in {"rate", "expense_rate"}:
        return f"{value:.2%}"
    if unit == "money":
        return f"${value:,.2f}" if currency == "USD" else f"{currency} {value:,.2f}"
    return f"{value:,.0f}"


def business_daily_card(db, store, day, *, settings=None, data_warning=None, reporting_day=None):
    context = business_daily_context(db, store.sid, day)
    if store.status != 1:
        return None
    if context["availability"] != "AVAILABLE":
        return api_business_card(db, store, day, reporting_day=reporting_day, data_warning=data_warning)
    rows = []
    for item in context["metrics"]:
        # An unavailable metric is explained once below, not as seven empty rows.
        if item["current"] is None:
            continue
        delta = item["change"]
        if data_warning:
            comparison = "待确认"
        elif not item["comparable"]:
            comparison = "币种不同"
        elif delta is None:
            comparison = "前值为零" if item["previous"] == 0 and item["current"] is not None else "—"
        else:
            comparison = f"{delta * 100:+.2f} 个百分点" if item["unit"].endswith("rate") else f"{delta:+.2%}"
        rows.append((item["label"], business_display(item["current"], item["unit"], item["currency"]),
            business_display(item["previous"], item["unit"], item["previous_currency"]), comparison))
    notes = []
    if any(r[0].endswith("¹") for r in rows):
        notes.append("¹ 领星经营看板口径，费用与退款占比按扣款绝对值展示。")
    if data_warning:
        notes.append(data_warning)
    currency = context["currency"]
    title = store.name[:60] + " 经营日报"
    day = date.fromisoformat(context["report_date"])
    label = date.fromisoformat(reporting_day) if isinstance(reporting_day, str) else reporting_day or day
    if reporting_day is not None:
        notes.insert(0, f"美国统计日：{day}")
    return card(title, subtitle=f"{label.year}年{label.month}月{label.day}日｜" +
        ("美元 USD" if currency == "USD" else currency or "币种按各行"),
        sections=[("", ["指标", "当日", "前一日", "日环比"], rows)],
        notes=[f"前一日：{context['previous_date']}", *notes])


def api_business_card(db, store, day, *, reporting_day=None, data_warning=None):
    """API evidence has its own labelled bases; it never fills dashboard facts."""
    from jx_alert.lingxing.api_business import api_business_context
    on = date.fromisoformat(day) if isinstance(day, str) else day
    current = api_business_context(db, store.sid, on)
    previous = api_business_context(db, store.sid, on - timedelta(days=1))
    specifications = [
        ("product", "volume", "销量（下单）", "count"),
        ("product", "netAmount", "净销售额（下单）", "money"),
        ("settlement", "platformIncome", "平台收入（结算）", "money"),
        ("settlement", "grossProfit", "毛利润（结算）", "money"),
        ("settlement", "grossRate", "毛利率（结算）", "rate"),
        ("product", "adsNetRatio", "广告费/净销售额", "rate"),
        ("settlement", "refundsRate", "退款率（结算销量）", "rate"),
    ]
    for packet in (current, previous):
        values = packet["product"].get("metrics", {})
        if values.get("netAmount", 0) > 0 and "adSpend" in values:
            values["adsNetRatio"] = abs(values["adSpend"]) / values["netAmount"]
    rows, has_prior = [], False
    for source, key, label, unit in specifications:
        a = current[source].get("metrics", {}).get(key)
        if a is None:
            continue
        b = previous[source].get("metrics", {}).get(key)
        currency = current[source].get("currency")
        if previous[source].get("currency") != currency:
            b = None
        has_prior |= b is not None
        change = "—"
        if b is not None and not data_warning:
            if unit == "rate":
                change = f"{(a-b)*100:+.2f}个百分点"
            elif b != 0:
                change = f"{(a-b)/abs(b):+.2%}"
            elif a == 0:
                change = "持平"
        rows.append((label, business_display(a, unit, currency), business_display(b, unit, currency), change))
    if not rows:
        return None
    label = date.fromisoformat(reporting_day) if isinstance(reporting_day, str) else reporting_day or on
    return card(store.name[:60] + " 接口经营日报", subtitle=f"{label.year}年{label.month}月{label.day}日｜美元 USD",
        sections=[("", ["指标", "当日", "前一日", "日环比"] if has_prior else ["指标", "当日"],
                   rows if has_prior else [row[:2] for row in rows])],
        notes=[f"美国统计日：{on}。销量及净销售额按下单日，财务指标按结算日。",
               *([data_warning] if data_warning else [])])
