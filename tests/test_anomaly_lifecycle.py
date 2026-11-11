from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import AnomalyEvent
from jx_alert.analysis.service import enqueue_anomaly_notifications, upsert_anomalies
from jx_alert.notifications.outbox import NotificationOutbox


def test_only_complete_scope_can_recover_missing_anomalies():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        row = AnomalyEvent(
            dedupe_key="store-1:sku-1:orders_decrease",
            store_id="store-1",
            rule="orders_decrease",
            severity="P1",
            status="ONGOING",
            evaluation_scope="store-1:sku-orders",
            first_seen=datetime.now(),
            last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
            metrics={"orders": 0},
            reason="order decrease",
        )
        db.add(row)
        db.commit()

        upsert_anomalies(db, "store-1", [], evaluation_scope="store-1:sku-orders",
                         scope_complete=False)
        db.commit()
        assert db.scalar(select(AnomalyEvent)).status == "ONGOING"

        upsert_anomalies(db, "store-1", [], evaluation_scope="store-1:sku-orders",
                         scope_complete=True)
        db.commit()
        recovered = db.scalar(select(AnomalyEvent))
        assert recovered.status == "RECOVERED"
        assert recovered.recovered_at is not None



def test_new_anomaly_is_queued_once_for_store_and_entity_routing():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        anomaly = AnomalyEvent(
            dedupe_key="store-1:ASIN1:orders_decrease",
            store_id="store-1",
            entity_id="ASIN1",
            rule="orders_decrease",
            severity="P1",
            status="NEW",
            evaluation_scope="store-1:sku-orders",
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            metrics={"current": 0},
            reason="orders decreased",
        )
        db.add(anomaly)
        db.flush()
        assert enqueue_anomaly_notifications(db, [anomaly]) == 1
        db.commit()
        queued = db.scalar(select(NotificationOutbox))
        assert queued.payload["store_id"] == "store-1"
        assert queued.payload["entity_id"] == "ASIN1"
        assert enqueue_anomaly_notifications(db, [anomaly]) == 1
        db.commit()
        from sqlalchemy import func
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1



def test_persistent_anomaly_escalates_once_and_keeps_escalated_severity():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row = AnomalyEvent(
            dedupe_key="store-1:sku-1:orders_decrease",
            store_id="store-1",
            rule="orders_decrease",
            severity="P2",
            status="ONGOING",
            evaluation_scope="store-1:sku-orders",
            first_seen=now - timedelta(days=3),
            last_seen=now - timedelta(days=1),
            metrics={"current": 5, "baseline_7d": 10},
            reason="orders decreased",
        )
        db.add(row)
        db.commit()

        event = {
            "rule": "orders_decrease",
            "severity": "P2",
            "entity_key": "sku-1",
            "dedupe_key": "sku-1:orders_decrease",
            "metrics": {"current": 5, "baseline_7d": 10},
            "reason": "orders decreased",
        }
        saved = upsert_anomalies(
            db,
            "store-1",
            [event],
            evaluation_scope="store-1:sku-orders",
            scope_complete=True,
            escalation_after_days=2,
        )
        assert saved[0].status == "ESCALATED"
        assert saved[0].severity == "P1"
        assert saved[0].escalated_at is not None
        assert enqueue_anomaly_notifications(db, saved) == 1
        first_escalated_at = saved[0].escalated_at
        db.commit()

        saved = upsert_anomalies(
            db,
            "store-1",
            [event],
            evaluation_scope="store-1:sku-orders",
            scope_complete=True,
            escalation_after_days=2,
        )
        assert saved[0].status == "ONGOING"
        assert saved[0].severity == "P1"
        assert saved[0].escalated_at == first_escalated_at
        assert enqueue_anomaly_notifications(db, saved) == 0
