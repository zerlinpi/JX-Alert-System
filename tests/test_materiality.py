from datetime import date, timedelta

import pytest

from jx_alert.analysis.materiality import material_change


DAY = date(2026, 11, 14)


def history(**overrides):
    row = dict(orders=10, units=12, sales=500, spend=100, clicks=100, impressions=10000,
               total_sales=1000, currency="USD", sales_currency="USD", total_sales_currency="USD")
    row.update(overrides)
    return {DAY - timedelta(days=i): dict(row) for i in range(8)}


def test_small_declines_and_high_percent_low_absolute_changes_stay_quiet():
    rows = history()
    rows[DAY]["orders"] = 8
    assert not material_change(rows, DAY, "orders")
    rows = history(orders=2)
    rows[DAY]["orders"] = 0
    assert not material_change(rows, DAY, "orders")


def test_ordinary_decline_needs_two_actual_dates_and_severe_decline_does_not():
    rows = history()
    rows[DAY]["orders"] = 6
    assert not material_change(rows, DAY, "orders")
    rows[DAY - timedelta(days=1)]["orders"] = 6
    assert material_change(rows, DAY, "orders")
    rows = history()
    rows[DAY]["orders"] = 4
    assert material_change(rows, DAY, "orders")


@pytest.mark.parametrize("metric", ["CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS"])
def test_stable_and_improving_metrics_never_alert(metric):
    assert not material_change(history(), DAY, metric)


def test_cpc_requires_absolute_impact_samples_and_persistence():
    rows = history()
    rows[DAY]["spend"] = 140
    assert not material_change(rows, DAY, "CPC")
    rows[DAY - timedelta(days=1)]["spend"] = 140
    assert material_change(rows, DAY, "CPC")
    rows[DAY]["clicks"] = 10
    assert not material_change(rows, DAY, "CPC")


def test_previous_day_fallback_does_not_invent_confirmation_or_missing_values():
    rows = {DAY: {"orders": 6}, DAY - timedelta(days=1): {"orders": 10}}
    assert not material_change(rows, DAY, "orders")
    rows[DAY - timedelta(days=2)] = {"orders": 16}
    assert material_change(rows, DAY, "orders")
    del rows[DAY - timedelta(days=1)]
    assert not material_change(rows, DAY, "orders")


@pytest.mark.parametrize("spend,clicks,orders,currency,expected", [
    (49, 30, 0, "USD", False), (50, 29, 0, "USD", False), (50, 30, 1, "USD", False),
    (50, 30, 0, "USD", True), (50, 30, None, "USD", False), (50, 30, 0, "EUR", False),
    (50, 30, 0, None, False), (float("nan"), 30, 0, "USD", False),
])
def test_zero_conversion_requires_real_usd_spend_clicks_and_explicit_zero(spend, clicks, orders, currency, expected):
    rows = {DAY: dict(spend=spend, clicks=clicks, orders=orders, currency=currency)}
    assert material_change(rows, DAY, "zero_conversion") is expected


def test_unknown_currency_and_undefined_ratios_do_not_alert():
    rows = history(currency=None)
    rows[DAY]["spend"] = 400
    assert not material_change(rows, DAY, "spend")
    rows = history()
    rows[DAY]["orders"] = 0
    assert not material_change(rows, DAY, "CPA")


def test_sales_and_spend_absolute_floors_cannot_be_bypassed_by_large_percentages():
    rows = history(sales=30, spend=10)
    rows[DAY].update(sales=0, spend=25)
    assert not material_change(rows, DAY, "sales")
    assert not material_change(rows, DAY, "spend")


def test_rule_outbox_is_quiet_until_material_and_recovers_only_delivered_incidents(tmp_path, monkeypatch):
    from datetime import datetime
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from jx_alert.analysis import service
    from jx_alert.config import Settings
    from jx_alert.storage.db import Base
    from jx_alert.storage.models import DailyMetric, DataCoverage, AnomalyEvent
    from jx_alert.notifications.outbox import NotificationOutbox
    settings = Settings(_env_file=None, alert_materiality_enabled=True,
        store_controls_file=str(tmp_path / "controls.json"))
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for offset in range(8):
            day = DAY - timedelta(days=offset)
            db.add(DataCoverage(store_id="s", dimension="store", metric_date=day, status="COMPLETE"))
            db.add(DailyMetric(store_id="s", dimension="store", entity_id="s", metric_name="orders",
                metric_date=day, value=8 if offset == 0 else 10))
        anomaly = AnomalyEvent(store_id="s", entity_id="s", rule="orders_decrease", severity="P1",
            status="NEW", dedupe_key="s:orders_decrease", evaluation_scope="s:sales:store",
            first_seen=datetime(2026, 11, 15), metrics={"report_date": DAY.isoformat()}, reason="订单下降")
        db.add(anomaly)
        db.flush()
        assert service.enqueue_anomaly_notifications(db, [anomaly]) == 0
        target = db.scalar(select(DailyMetric).where(DailyMetric.metric_date == DAY))
        target.value = 0
        anomaly.status = "ONGOING"
        assert service.enqueue_anomaly_notifications(db, [anomaly]) == 1
        assert service.enqueue_anomaly_notifications(db, [anomaly]) == 0
        anomaly.status = "RECOVERED"
        assert service.enqueue_anomaly_notifications(db, [anomaly]) == 0
        db.scalar(select(NotificationOutbox)).status = "DELIVERED"
        assert service.enqueue_anomaly_notifications(db, [anomaly]) == 1
    engine.dispose()
