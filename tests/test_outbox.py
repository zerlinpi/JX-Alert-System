from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.base import Base
from jx_alert.storage import models  # noqa: F401 - register all ORM tables
from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification

DELIVERY = {"role": "jx_daily_lead", "receive_id_type": "open_id", "receive_id": "ou_lead", "open_ids": []}


def test_outbox_enqueue_is_idempotent_and_freezes_the_first_payload():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        first = enqueue_notification(db, idempotency_key="jx:daily:2026-11-24:abc", text="日报",
                                     delivery=DELIVERY, card={"schema": "2.0"}, extra={"jx_lane": "daily"})
        second = enqueue_notification(db, idempotency_key="jx:daily:2026-11-24:abc", text="duplicate",
                                      delivery={**DELIVERY, "receive_id": "ou_other"})
        db.commit()
        assert first.id == second.id
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        row = db.get(NotificationOutbox, first.id)
        assert row.chat_id == "ou_lead" and row.payload["text"] == "日报"
        assert row.payload["transport"] == "app" and row.payload["jx_lane"] == "daily"
