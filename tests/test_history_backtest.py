from datetime import date, timedelta

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from jx_alert.analysis.backtest import backtest_history
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AdProduct, Campaign, Store, DailyMetric, DataCoverage, AnomalyEvent,
)
from jx_alert.notifications.outbox import NotificationOutbox


def test_real_evaluator_replay_keeps_source_warehouse_and_outbox_untouched(monkeypatch):
    import jx_alert.analysis.service as service
    from jx_alert.config import Settings
    monkeypatch.setattr(service, "get_settings", lambda: Settings(_env_file=None))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 1, 20)
    with Session(engine) as db:
        db.add(Store(sid="1", name="Test", status=1, timezone="America/Los_Angeles"))
        for offset in range(8):
            day = target - timedelta(days=offset)
            db.add(DailyMetric(store_id="1", metric_date=day, dimension="store", entity_id="1",
                               metric_name="orders", value=0 if offset == 0 else 10))
            db.add(DataCoverage(store_id="1", metric_date=day, dimension="store",
                                status="COMPLETE", row_count=1))
        db.commit()
        result = backtest_history(db, store_id="1", start_date=str(target), end_date=str(target))
        assert result["candidate_count"] == 1
        assert result["candidates"][0]["rule"] == "orders_decrease"
        assert result["false_positive_rate"] is None
        assert db.scalar(select(func.count()).select_from(AnomalyEvent)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        key = result["candidates"][0]["key"]
        labeled = backtest_history(db, store_id="1", start_date=str(target), end_date=str(target),
                                    labels={key: "false_positive"})
        assert labeled["false_positive_rate"] == 1


def test_same_sku_ads_keep_independent_candidate_labels_and_notification_keys(monkeypatch):
    import jx_alert.analysis.service as service
    import jx_alert.config as config_module
    from jx_alert.config import Settings

    settings = Settings(_env_file=None, feishu_transport="webhook")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    notification_keys = []
    enqueue = service.enqueue_notification

    def capture_notification(db, **kwargs):
        notification_keys.append(kwargs["idempotency_key"])
        return enqueue(db, **kwargs)

    monkeypatch.setattr(service, "enqueue_notification", capture_notification)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 1, 20)
    with Session(engine) as db:
        db.add(Store(sid="1", name="Test", status=1, timezone="America/Los_Angeles"))
        for ad_id, campaign_id, spend in (("1001", "2001", 30), ("1002", "2002", 40)):
            db.add(Campaign(store_id="1", campaign_type="sp", campaign_id=campaign_id,
                            name=campaign_id, state="enabled"))
            db.add(AdProduct(store_id="1", campaign_type="sp", ad_id=ad_id,
                             campaign_id=campaign_id, seller_sku="SHARED-SKU",
                             asin="B000000001", state="enabled"))
            for offset in range(15):
                day = target - timedelta(days=offset)
                for metric_name, value in {
                    "spend": spend if offset == 0 else 10,
                    "orders": 2, "clicks": 10,
                }.items():
                    db.add(DailyMetric(store_id="1", metric_date=day,
                                       dimension="product_ad_sp", entity_id=ad_id,
                                       metric_name=metric_name, value=value))
        for offset in range(15):
            db.add(DataCoverage(store_id="1", metric_date=target - timedelta(days=offset),
                                dimension="product_ad_sp", status="COMPLETE", row_count=2))
        db.commit()

        first = backtest_history(db, store_id="1", start_date=str(target), end_date=str(target))
        assert first["candidate_count"] == 2
        assert {row["entity_id"] for row in first["candidates"]} == {"SHARED-SKU"}
        keys = {row["metrics"]["ad_id"]: row["key"] for row in first["candidates"]}
        assert len(set(keys.values())) == 2
        assert len(set(notification_keys)) == 2
        assert all(any(f":{ad_id}:" in key for key in notification_keys)
                   for ad_id in ("1001", "1002"))

        labeled = backtest_history(
            db, store_id="1", start_date=str(target), end_date=str(target),
            labels={keys["1001"]: "false_positive"},
        )
        labels = {row["metrics"]["ad_id"]: row["review_label"]
                  for row in labeled["candidates"]}
        assert labels == {"1001": "false_positive", "1002": None}
        assert labeled["false_positive"] == 1 and labeled["unreviewed"] == 1
        assert db.scalar(select(func.count()).select_from(AnomalyEvent)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    engine.dispose()
