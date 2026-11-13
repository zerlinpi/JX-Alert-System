from datetime import date, datetime, timedelta, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import (
    AdProduct,
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    Product,
    Store,
)
from jx_alert.analysis.evaluator import (
    _evaluate_campaign_dimension,
    _evaluate_product_ad_dimension,
    evaluate_daily_anomalies,
)
from jx_alert.analysis.rules import RuleConfig


def _add_coverage(db, store_id, dimension, report_date, days=8):
    for offset in range(days - 1, -1, -1):
        db.add(DataCoverage(
            store_id=store_id,
            metric_date=report_date - timedelta(days=offset),
            dimension=dimension,
            status="COMPLETE",
            row_count=1,
        ))


def _add_metric(db, store_id, dimension, entity_id, metric_name, metric_date, value):
    db.add(DailyMetric(
        store_id=store_id,
        metric_date=metric_date,
        dimension=dimension,
        entity_id=entity_id,
        metric_name=metric_name,
        value=value,
        currency="USD",
        source="test",
    ))


def test_evaluator_detects_sales_campaign_and_product_ad_anomalies():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    target = date(2026, 11, 10)
    store_id = "store-1"

    with Session() as db:
        db.add(Store(sid=store_id, name="Store A", status=1, has_ads_setting=1))
        db.add(Product(
            store_id=store_id, seller_sku="SKU-1", asin="B000000001",
            product_name="Product",
        ))
        db.add(Campaign(
            store_id=store_id, campaign_type="sp", campaign_id="campaign-1",
            name="SP Campaign", state="enabled", budget=25, budget_type="daily",
            last_over_budget_at="2026-11-10 12:00:00",
        ))
        db.add(AdProduct(
            store_id=store_id, campaign_type="sp", ad_id="ad-1",
            campaign_id="campaign-1", seller_sku="SKU-1", asin="B000000001",
            state="enabled",
        ))

        dimensions = [
            "store", "sku_msku", "sku_asin",
            "campaign_sp", "campaign_sb", "campaign_sd",
            "product_ad_sp", "product_ad_sd",
        ]
        for dimension in dimensions:
            days = (
                22 if dimension in {
                    "campaign_sb", "campaign_sd", "product_ad_sd"
                } else 15 if dimension in {
                    "campaign_sp", "product_ad_sp"
                } else 8
            )
            _add_coverage(db, store_id, dimension, target, days=days)

        for offset in range(7, 0, -1):
            day = target - timedelta(days=offset)
            for dimension, entity in (
                ("store", store_id),
                ("sku_msku", "SKU-1"),
                ("sku_asin", "B000000001"),
            ):
                _add_metric(db, store_id, dimension, entity, "orders", day, 10)
                _add_metric(db, store_id, dimension, entity, "units", day, 10)
                _add_metric(db, store_id, dimension, entity, "sales", day, 100)
            spend_value, order_value, click_value = (
                (25, 0, 15) if offset == 7 else (10, 2, 20)
            )
            _add_metric(db, store_id, "campaign_sp", "campaign-1", "spend", day, spend_value)
            _add_metric(db, store_id, "campaign_sp", "campaign-1", "orders", day, order_value)
            _add_metric(db, store_id, "campaign_sp", "campaign-1", "clicks", day, click_value)
            _add_metric(db, store_id, "product_ad_sp", "ad-1", "spend", day, spend_value)
            _add_metric(db, store_id, "product_ad_sp", "ad-1", "orders", day, order_value)
            _add_metric(db, store_id, "product_ad_sp", "ad-1", "clicks", day, click_value)

        # Seven fully matured baseline dates before the attribution-complete day.
        for offset in range(14, 7, -1):
            day = target - timedelta(days=offset)
            for dimension, entity in (
                ("campaign_sp", "campaign-1"),
                ("product_ad_sp", "ad-1"),
            ):
                _add_metric(db, store_id, dimension, entity, "spend", day, 10)
                _add_metric(db, store_id, dimension, entity, "orders", day, 2)
                _add_metric(db, store_id, dimension, entity, "clicks", day, 20)

        for dimension, entity in (
            ("store", store_id),
            ("sku_msku", "SKU-1"),
            ("sku_asin", "B000000001"),
        ):
            _add_metric(db, store_id, dimension, entity, "orders", target, 5)
            _add_metric(db, store_id, dimension, entity, "units", target, 15)
            _add_metric(db, store_id, dimension, entity, "sales", target, 50)
        _add_metric(db, store_id, "campaign_sp", "campaign-1", "spend", target, 30)
        # Today's orders are intentionally positive but still attribution-incomplete.
        _add_metric(db, store_id, "campaign_sp", "campaign-1", "orders", target, 10)
        _add_metric(db, store_id, "campaign_sp", "campaign-1", "clicks", target, 15)
        _add_metric(db, store_id, "product_ad_sp", "ad-1", "spend", target, 25)
        _add_metric(db, store_id, "product_ad_sp", "ad-1", "orders", target, 10)
        _add_metric(db, store_id, "product_ad_sp", "ad-1", "clicks", target, 15)
        db.commit()

        result = evaluate_daily_anomalies(db, target)
        assert result["evaluated_scopes"] == 5
        assert result["skipped_incomplete_scopes"] == 3
        events = db.scalars(select(AnomalyEvent)).all()
        assert any(row.rule == "orders_decrease" and row.entity_id == "SKU-1" for row in events)
        assert any(row.rule == "units_increase" and row.entity_id == "B000000001" for row in events)
        assert any(row.rule == "sales_decrease" and row.entity_id == "SKU-1" for row in events)
        assert any(row.rule == "high_ad_spend" and row.entity_id == "campaign-1" for row in events)
        assert any(row.rule == "campaign_over_budget" for row in events)
        assert any(row.rule == "spend_no_conversion" and row.entity_id == "SKU-1" for row in events)
        outbox_rows = db.scalars(select(NotificationOutbox)).all()
        assert outbox_rows
        notification_texts = [row.payload["text"] for row in outbox_rows]
        assert any("Store A" in text and "SP Campaign" in text for text in notification_texts)
        assert any("SKU-1" in text and "B000000001" in text for text in notification_texts)


def test_incomplete_coverage_does_not_recover_existing_anomaly():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    target = date(2026, 11, 10)
    store_id = "store-1"

    with Session() as db:
        db.add(Store(sid=store_id, name="Store A", status=1))
        db.add(Product(store_id=store_id, seller_sku="SKU-1", asin="B000000001"))
        db.add(AnomalyEvent(
            dedupe_key="store-1:SKU-1:orders_decrease",
            store_id=store_id,
            rule="orders_decrease",
            entity_id="SKU-1",
            severity="P1",
            status="ONGOING",
            evaluation_scope="store-1:sales:sku_msku",
            first_seen=datetime.now(timezone.utc).replace(tzinfo=None),
            last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
            metrics={"baseline_7d": 10},
            reason="previous alert",
        ))
        for offset in range(7, 0, -1):
            db.add(DataCoverage(
                store_id=store_id,
                metric_date=target - timedelta(days=offset),
                dimension="sku_msku",
                status="COMPLETE",
                row_count=1,
            ))
        db.commit()

        result = evaluate_daily_anomalies(db, target)
        assert result["evaluated_scopes"] == 0
        assert result["skipped_incomplete_scopes"] == 8
        row = db.scalar(select(AnomalyEvent))
        assert row.status == "ONGOING"
        assert db.scalars(select(NotificationOutbox)).all() == []


def test_evaluator_uses_each_stores_marketplace_local_report_date():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    store_dates = {
        "us-store": date(2026, 11, 10),
        "jp-store": date(2026, 11, 9),
    }

    with Session() as db:
        for store_id, target in store_dates.items():
            db.add(Store(sid=store_id, name=store_id, status=1))
            _add_coverage(db, store_id, "store", target)
            for offset in range(7, 0, -1):
                _add_metric(
                    db, store_id, "store", store_id, "orders",
                    target - timedelta(days=offset), 10,
                )
            _add_metric(db, store_id, "store", store_id, "orders", target, 5)
        db.commit()

        result = evaluate_daily_anomalies(
            db,
            {store_id: target.isoformat() for store_id, target in store_dates.items()},
        )

        assert result["stores_seen"] == 2
        assert result["report_date"] is None
        assert result["report_dates"] == {
            "us-store": "2026-11-10",
            "jp-store": "2026-11-09",
        }
        events = db.scalars(select(AnomalyEvent).where(
            AnomalyEvent.rule == "orders_decrease"
        )).all()
        assert {event.store_id for event in events} == {"us-store", "jp-store"}
        assert all(event.metrics["current"] == 5 for event in events)



def test_product_ad_anomaly_uses_configured_escalation_window():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    target = date(2026, 11, 10)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with Session() as db:
        db.add(Campaign(
            store_id="store-1", campaign_type="sp", campaign_id="campaign-1",
            name="SP", state="enabled",
        ))
        db.add(AdProduct(
            store_id="store-1", campaign_type="sp", ad_id="ad-1",
            campaign_id="campaign-1", seller_sku="SKU-1", asin="B000000001",
            state="enabled",
        ))
        _add_coverage(db, "store-1", "product_ad_sp", target, days=15)
        for offset in range(14, 7, -1):
            day = target - timedelta(days=offset)
            _add_metric(db, "store-1", "product_ad_sp", "ad-1", "spend", day, 10)
            _add_metric(db, "store-1", "product_ad_sp", "ad-1", "orders", day, 2)
            _add_metric(db, "store-1", "product_ad_sp", "ad-1", "clicks", day, 20)
        mature_day = target - timedelta(days=7)
        _add_metric(db, "store-1", "product_ad_sp", "ad-1", "spend", mature_day, 25)
        _add_metric(db, "store-1", "product_ad_sp", "ad-1", "orders", mature_day, 0)
        _add_metric(db, "store-1", "product_ad_sp", "ad-1", "clicks", mature_day, 15)
        _add_metric(db, "store-1", "product_ad_sp", "ad-1", "spend", target, 10)
        _add_metric(db, "store-1", "product_ad_sp", "ad-1", "spend", target - timedelta(days=1), 10)
        _add_metric(db, "store-1", "product_ad_sp", "ad-1", "clicks", target, 10)
        existing = AnomalyEvent(
            dedupe_key="store-1:ad-1:spend_no_conversion",
            store_id="store-1",
            rule="spend_no_conversion",
            entity_id="SKU-1",
            severity="P1",
            status="ONGOING",
            evaluation_scope="store-1:product_ad:sp",
            first_seen=now - timedelta(days=3),
            last_seen=now - timedelta(days=1),
            metrics={"spend": 25},
            reason="existing product-ad anomaly",
        )
        db.add(existing)
        db.commit()

        saved, complete = _evaluate_product_ad_dimension(
            db,
            "store-1",
            "sp",
            target,
            RuleConfig(escalate_after_days=5, min_spend=20, min_clicks=10),
        )
        assert complete is True
        updated = next(row for row in saved if row.rule == "spend_no_conversion")
        assert updated.status == "ONGOING"
        assert updated.escalated_at is None



def test_incomplete_mature_attribution_coverage_does_not_recover_campaign_alert():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    target = date(2026, 11, 10)

    with Session() as db:
        db.add(Campaign(
            store_id="store-1", campaign_type="sp", campaign_id="campaign-1",
            name="SP", state="enabled",
        ))
        existing = AnomalyEvent(
            dedupe_key="store-1:campaign-1:orders_decrease",
            store_id="store-1",
            rule="orders_decrease",
            entity_id="campaign-1",
            severity="P1",
            status="ONGOING",
            evaluation_scope="store-1:campaign:sp",
            first_seen=datetime.now(timezone.utc).replace(tzinfo=None),
            last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
            metrics={"baseline_7d": 10},
            reason="previous mature-attribution alert",
        )
        db.add(existing)
        # Latest eight days are complete, but the mature conversion baseline
        # (D-14 through D-8 for SP) is missing.
        _add_coverage(db, "store-1", "campaign_sp", target, days=8)
        db.commit()

        saved, complete = _evaluate_campaign_dimension(
            db, "store-1", "sp", target, RuleConfig()
        )
        assert complete is False
        assert saved == []
        assert existing.status == "ONGOING"
