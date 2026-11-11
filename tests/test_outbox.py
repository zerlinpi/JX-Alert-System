from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401 - register all ORM tables
from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification


def test_outbox_enqueue_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        first = enqueue_notification(
            db, idempotency_key="store:sku:rule:2026-11-11",
            text="alert", open_ids=["ou_test"],
        )
        second = enqueue_notification(
            db, idempotency_key="store:sku:rule:2026-11-11",
            text="duplicate", open_ids=[],
        )
        db.commit()
        assert first.id == second.id
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
