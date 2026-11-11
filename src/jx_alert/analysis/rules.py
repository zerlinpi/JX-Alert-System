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

def _ratio_change(current: float, baseline: float) -> float | None:
    if baseline <= 0:
        return None
    return (current - baseline) / baseline

def _event(kind: str, severity: str, key: str, metrics: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"rule": kind, "severity": severity, "dedupe_key": key,
            "metrics": metrics, "reason": reason, "status": "NEW"}

def detect_order_anomaly(*, entity_key: str, current_orders: float,
                         previous_orders: float, prior_7_days: list[float],
                         config: RuleConfig) -> list[dict[str, Any]]:
    if len(prior_7_days) != 7:
        return []
    baseline = sum(prior_7_days) / 7
    if baseline < config.min_orders_baseline:
        return []
    delta = _ratio_change(current_orders, baseline)
    if delta is None or abs(delta) < config.order_change_ratio:
        return []
    direction = "increase" if delta > 0 else "decrease"
    return [_event(f"orders_{direction}", "P2" if delta > 0 else "P1",
                   f"{entity_key}:orders_{direction}",
                   {"current": current_orders, "previous": previous_orders,
                    "baseline_7d": baseline, "change_ratio": delta},
                   f"订单量相对此前七个完整自然日均值变化 {delta:.1%}")]

def detect_campaign_anomalies(*, entity_key: str, spend: float, orders: float,
                              clicks: int, prior_7_spend: list[float],
                              prior_7_orders: list[float], daily_budget: float | None,
                              config: RuleConfig, campaign_enabled: bool = True
                              ) -> list[dict[str, Any]]:
    if len(prior_7_spend) != 7 or len(prior_7_orders) != 7:
        return []
    events: list[dict[str, Any]] = []
    spend_avg = sum(prior_7_spend) / 7
    orders_avg = sum(prior_7_orders) / 7
    if spend_avg > 0 and spend >= config.min_spend and spend / spend_avg >= config.high_spend_ratio:
        events.append(_event("high_ad_spend", "P1", f"{entity_key}:high_ad_spend",
            {"spend": spend, "spend_avg_7d": spend_avg, "orders": orders},
            f"广告花费达到七日均值的 {spend / spend_avg:.2f} 倍"))
    if spend >= config.min_spend and clicks >= config.min_clicks and orders == 0 and orders_avg > 0:
        events.append(_event("spend_no_conversion", "P1", f"{entity_key}:spend_no_conversion",
            {"spend": spend, "clicks": clicks, "orders": orders, "orders_avg_7d": orders_avg},
            "广告花费和点击达到观察门槛，但广告订单为 0"))
    if campaign_enabled and spend_avg > 0 and spend < spend_avg * config.low_spend_ratio and orders == 0:
        events.append(_event("low_spend_no_conversion", "P3", f"{entity_key}:low_spend_no_conversion",
            {"spend": spend, "spend_avg_7d": spend_avg, "orders": orders},
            "启用中的广告活动花费低于七日均值且没有转化，需结合投放量检查"))
    if daily_budget and daily_budget > 0 and spend / daily_budget >= config.budget_ratio:
        events.append(_event("budget_consumption", "P2", f"{entity_key}:budget_consumption",
            {"spend": spend, "daily_budget": daily_budget, "budget_ratio": spend / daily_budget},
            "当日花费达到配置的预算消耗提醒比例；不等同于平台超预算违规"))
    return events
