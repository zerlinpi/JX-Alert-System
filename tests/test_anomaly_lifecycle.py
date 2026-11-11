from datetime import datetime
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
