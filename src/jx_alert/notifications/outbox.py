"""Persistent notification outbox and idempotent enqueue helper."""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from jx_alert.storage.base import Base

QUESTION_REPLY_PREFIX = "group-question:"  # group and private replies share the namespace
PRE_SEND_RETRY = "PRE_SEND_RETRY"
CLAIMABLE = frozenset({"PENDING", "RETRY", PRE_SEND_RETRY})
# SENDING is the legacy phase: it did not distinguish preparation from POST.
IN_FLIGHT = CLAIMABLE | {"SENDING", "PREPARING", "SUBMITTING"}
DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
RESERVING = IN_FLIGHT | {DELIVERY_UNKNOWN}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(200), default="default")
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


def enqueue_notification(db, *, idempotency_key: str, text: str, delivery: dict,
                         card: dict | None = None, extra: dict | None = None) -> NotificationOutbox:
    """Insert once per idempotency key, safely handling concurrent producers.

    ``delivery`` is the frozen Feishu app destination: role, receive_id_type,
    receive_id, open_ids (explicit mentions only) and optional reply target.
    """
    existing = db.scalar(select(NotificationOutbox).where(
        NotificationOutbox.idempotency_key == idempotency_key))
    if existing:
        return existing
    payload = {"text": text, "transport": "app", "delivery": delivery, "open_ids": []}
    if card is not None:
        payload["card"] = card
    if extra:
        payload.update(extra)
    row = NotificationOutbox(idempotency_key=idempotency_key, chat_id=delivery["receive_id"],
                             payload=payload, status="PENDING")
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # MariaDB REPEATABLE READ: the old snapshot may still miss the winner.
        # Only after a unique-key collision use a current read.
        existing = db.scalar(select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key == idempotency_key).with_for_update())
        if existing:
            return existing
        raise
