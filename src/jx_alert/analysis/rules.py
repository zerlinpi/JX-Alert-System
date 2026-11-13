from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleConfig:
    order_change_ratio: float = 0.15
    high_spend_ratio: float = 1.30
    low_spend_ratio: float = 0.50
    budget_ratio: float = 0.90
    min_spend: float = 20.0
    min_clicks: int = 10
    min_orders_baseline: float = 1.0
    escalate_after_days: int = 2


def _ratio_change(current: float, baseline: float) -> float | None:
    if baseline <= 0:
        return None
    return (current - baseline) / baseline


def _event(kind: str, severity: str, entity_key: str, key: str,
           metrics: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "rule": kind,
        "severity": severity,
        "entity_key": entity_key,
        "dedupe_key": key,
        "metrics": metrics,
        "reason": reason,
        "status": "NEW",
    }


def detect_order_anomaly(*, entity_key: str, current_orders: float,
                         previous_orders: float | None, prior_7_days: list[float],
                         config: RuleConfig, metric_name: str = "orders",
                         comparison_basis: str | None = None,
                         ) -> list[dict[str, Any]]:
    basis = comparison_basis or ("SEVEN_DAY" if len(prior_7_days) == 7 else "PREVIOUS_DAY")
    if basis == "SEVEN_DAY" and len(prior_7_days) == 7:
        baseline = sum(prior_7_days) / 7
        reference_metrics = {"baseline_7d": baseline}
        label = "此前七个完整自然日均值"
    elif basis == "PREVIOUS_DAY" and previous_orders is not None:
        baseline = previous_orders
        reference_metrics = {}
        label = "前一日"
    else:
        return []
    if baseline < config.min_orders_baseline:
        return []
    delta = _ratio_change(current_orders, baseline)
    if delta is None or abs(delta) < config.order_change_ratio:
        return []
    direction = "increase" if delta > 0 else "decrease"
    return [
        _event(
            f"{metric_name}_{direction}",
            "P2" if delta > 0 else "P1",
            entity_key,
            f"{entity_key}:{metric_name}_{direction}",
            {
                "metric": metric_name,
                "current": current_orders,
                "previous": previous_orders,
                "comparison_basis": basis,
                **reference_metrics,
                "change_ratio": delta,
            },
            f"{ {'orders': '订单量', 'units': '销量', 'sales': '销售额'}.get(metric_name, metric_name) }相对{label}变化 {delta:.1%}",
        )
    ]


def detect_campaign_anomalies(*, entity_key: str, spend: float, orders: float | None,
                              clicks: int | None, prior_7_spend: list[float],
                              prior_7_orders: list[float], daily_budget: float | None,
                              config: RuleConfig, campaign_enabled: bool = True,
                              high_spend_enabled: bool = True,
                              conversion_enabled: bool = True,
                              budget_enabled: bool = True,
                              previous_spend: float | None = None,
                              previous_orders: float | None = None,
                              comparison_basis: str | None = None,
                              ) -> list[dict[str, Any]]:
    basis = comparison_basis or (
        "SEVEN_DAY" if len(prior_7_spend) == 7 and (not conversion_enabled or len(prior_7_orders) == 7)
        else "PREVIOUS_DAY")
    if basis == "SEVEN_DAY" and len(prior_7_spend) == 7 and (not conversion_enabled or len(prior_7_orders) == 7):
        spend_avg = sum(prior_7_spend) / 7
        orders_avg = sum(prior_7_orders) / 7 if len(prior_7_orders) == 7 else None
        spend_reference = {"spend_avg_7d": spend_avg}
        order_reference = {"orders_avg_7d": orders_avg} if orders_avg is not None else {}
        label = "七日均值"
    elif basis == "PREVIOUS_DAY" and previous_spend is not None and (not conversion_enabled or previous_orders is not None):
        spend_avg, orders_avg = previous_spend, previous_orders
        spend_reference = {"spend_previous": previous_spend}
        order_reference = {"orders_previous": previous_orders} if previous_orders is not None else {}
        label = "前一日"
    else:
        return []
    events: list[dict[str, Any]] = []

    if (high_spend_enabled and spend_avg > 0 and spend >= config.min_spend
            and spend / spend_avg >= config.high_spend_ratio):
        events.append(_event(
            "high_ad_spend", "P1", entity_key, f"{entity_key}:high_ad_spend",
            {"spend": spend, **spend_reference, "orders": orders},
            f"广告花费达到{label}的 {spend / spend_avg:.2f} 倍",
        ))
    if (conversion_enabled and campaign_enabled and spend >= config.min_spend
            and clicks is not None and clicks >= config.min_clicks and orders == 0):
        events.append(_event(
            "spend_no_conversion", "P1", entity_key, f"{entity_key}:spend_no_conversion",
            {"spend": spend, "clicks": clicks, "orders": orders, **order_reference},
            "广告花费和点击达到观察门槛，但广告订单为 0",
        ))
    if (conversion_enabled and campaign_enabled and spend_avg > 0
            and config.min_spend <= spend < spend_avg * config.low_spend_ratio
            and clicks is not None and clicks >= config.min_clicks and orders == 0):
        events.append(_event(
            "low_spend_no_conversion", "P3", entity_key, f"{entity_key}:low_spend_no_conversion",
            {"spend": spend, **spend_reference, "clicks": clicks, "orders": orders},
            f"启用中的广告活动花费显著低于{label}，花费与点击达到观察门槛但没有转化",
        ))
    if (budget_enabled and daily_budget and daily_budget > 0
            and spend / daily_budget >= config.budget_ratio):
        events.append(_event(
            "budget_consumption", "P2", entity_key, f"{entity_key}:budget_consumption",
            {"spend": spend, "daily_budget": daily_budget, "budget_ratio": spend / daily_budget},
            "当日花费达到配置的预算消耗提醒比例；不等同于平台超预算违规",
        ))
    for event in events:
        event["metrics"]["comparison_basis"] = basis
    return events
