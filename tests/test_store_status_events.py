from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import AnomalyEvent
from jx_alert.analysis.evaluator import evaluate_store_status_events


def test_store_status_event_is_queued_and_recovers_when_status_returns_to_normal():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        first = evaluate_store_status_events(db, {
            "data": [{"sid": 101, "status": 2, "has_ads_setting": 1}]
        })
        assert first["non_normal_stores"] == 1
        row = db.scalar(select(AnomalyEvent))
        assert row.rule == "store_status_not_normal"
        assert row.severity == "P1"
        assert row.status == "NEW"

        second = evaluate_store_status_events(db, {
            "data": [{"sid": 101, "status": 1, "has_ads_setting": 1}]
        })
        assert second["non_normal_stores"] == 0
        assert row.status == "RECOVERED"
        assert db.scalars(select(NotificationOutbox)).all()
