"""Persistent notification outbox and idempotent enqueue helper."""
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text, JSON, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from jx_alert.storage.db import Base


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(200), default="default")
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


def enqueue_notification(db, *, idempotency_key: str, text: str,
                         store_id: str | None = None, entity_id: str | None = None,
                         chat_id: str = "default",
                         open_ids: list[str] | None = None,
                         delivery: dict | None = None,
                         card: dict | None = None,
                         fixed_route: bool = False) -> NotificationOutbox:
    """Insert once per idempotency key, safely handling concurrent producers."""
    existing = db.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key == idempotency_key
        )
    )
    if existing:
        return existing

    row = NotificationOutbox(
        idempotency_key=idempotency_key,
        chat_id=chat_id,
        payload={"text": text, "store_id": store_id, "entity_id": entity_id,
                 "open_ids": open_ids or []},
        status="PENDING",
    )
    if fixed_route:
        row.payload = {**row.payload, "fixed_route": True}
    if card is not None:
        row.payload = {**row.payload, "card": card}
    if delivery is not None:
        row.payload = {**row.payload, "transport": "app", "delivery": delivery}
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # A concurrent producer may have inserted the unique key after our SELECT.
        existing = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == idempotency_key
            )
        )
        if existing:
            return existing
        raise
