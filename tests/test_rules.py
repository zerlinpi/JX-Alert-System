from jx_alert.analysis.rules import RuleConfig, detect_order_anomaly, detect_campaign_anomalies
import pytest

def test_order_change_uses_prior_seven_days():
    result = detect_order_anomaly(entity_key="store:sku", current_orders=12,
        previous_orders=10, prior_7_days=[10]*7, config=RuleConfig())
    assert len(result) == 1
    assert result[0]["rule"] == "orders_increase"
    assert result[0]["metrics"]["baseline_7d"] == 10

def test_order_small_change_does_not_alert():
    assert detect_order_anomaly(entity_key="x", current_orders=11,
        previous_orders=10, prior_7_days=[10]*7, config=RuleConfig()) == []

def test_insufficient_seven_days_can_use_observed_previous_day():
    result = detect_order_anomaly(entity_key="x", current_orders=20,
        previous_orders=10, prior_7_days=[10]*6, config=RuleConfig())
    assert result[0]["metrics"]["comparison_basis"] == "PREVIOUS_DAY"
    assert "baseline_7d" not in result[0]["metrics"]
    assert detect_order_anomaly(entity_key="x", current_orders=20,
        previous_orders=None, prior_7_days=[10]*6, config=RuleConfig()) == []

def test_campaign_spend_and_zero_conversion_rules():
    events = detect_campaign_anomalies(entity_key="store:campaign", spend=50, orders=0,
        clicks=20, prior_7_spend=[20]*7, prior_7_orders=[2]*7, daily_budget=60,
        config=RuleConfig(min_spend=20, min_clicks=10))
    rules = {e["rule"] for e in events}
    assert "high_ad_spend" in rules
    assert "spend_no_conversion" in rules



def test_paused_campaign_does_not_trigger_zero_conversion_alert():
    events = detect_campaign_anomalies(
        entity_key="campaign-paused",
        spend=50,
        orders=0,
        clicks=20,
        prior_7_spend=[20] * 7,
        prior_7_orders=[2] * 7,
        daily_budget=60,
        config=RuleConfig(min_spend=20, min_clicks=10),
        campaign_enabled=False,
    )
    rules = {event["rule"] for event in events}
    assert "high_ad_spend" in rules
    assert "spend_no_conversion" not in rules
    assert "low_spend_no_conversion" not in rules


@pytest.mark.parametrize("current,expected", [(11.5, "orders_increase"), (8.5, "orders_decrease")])
def test_exact_fifteen_percent_order_change_is_included(current, expected):
    events = detect_order_anomaly(
        entity_key="sku", current_orders=current, previous_orders=10,
        prior_7_days=[10] * 7, config=RuleConfig(),
    )
    assert [event["rule"] for event in events] == [expected]


def test_mature_zero_conversion_does_not_require_past_conversions():
    events = detect_campaign_anomalies(
        entity_key="campaign", spend=40, orders=0, clicks=20,
        prior_7_spend=[20] * 7, prior_7_orders=[0] * 7, daily_budget=None,
        config=RuleConfig(),
    )
    assert "spend_no_conversion" in {event["rule"] for event in events}


@pytest.mark.parametrize("spend,clicks,expected", [(25, 20, True), (2, 20, False), (25, 1, False)])
def test_low_spend_zero_conversion_requires_enough_observed_sample(spend, clicks, expected):
    events = detect_campaign_anomalies(
        entity_key="campaign", spend=spend, orders=0, clicks=clicks,
        prior_7_spend=[100] * 7, prior_7_orders=[0] * 7, daily_budget=None,
        config=RuleConfig(),
    )
    assert ("low_spend_no_conversion" in {event["rule"] for event in events}) == expected
