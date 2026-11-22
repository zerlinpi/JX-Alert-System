"""Persistent notification outbox and idempotent enqueue helper."""
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text, JSON, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from jx_alert.storage.db import Base


QUESTION_REPLY_PREFIX = "group-question:"  # group and private replies share the namespace
PRE_SEND_RETRY = "PRE_SEND_RETRY"
CLAIMABLE = frozenset({"PENDING", "RETRY", PRE_SEND_RETRY})
# SENDING is the legacy phase: it did not distinguish preparation from POST.
IN_FLIGHT = CLAIMABLE | {"SENDING", "PREPARING", "SUBMITTING"}
DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
RESERVING = IN_FLIGHT | {DELIVERY_UNKNOWN}


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
                         fixed_route: bool = False,
                         store_ids: list[str] | None = None,
                         material_ad_alert: bool = False,
                         rule_scope: dict | None = None,
                         failed_route: dict | None = None) -> NotificationOutbox:
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
    if store_ids is not None:
        row.payload = {**row.payload, "store_ids": store_ids}
    if card is not None:
        row.payload = {**row.payload, "card": card}
    if material_ad_alert:
        row.payload = {**row.payload, "material_ad_alert": True}
    if delivery is not None:
        row.payload = {**row.payload, "transport": "app", "delivery": delivery}
    # Producer provenance is insert-only, including a concurrent insert loser.
    if rule_scope is not None:
        row.payload = {**row.payload, "rule_scope": rule_scope}
    if failed_route is not None:
        row.payload = {**row.payload, **failed_route}
        row.status = "ROUTING_FAILED"
        row.last_error = delivery["routing_failure"]
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
            if failed_route is not None:
                from jx_alert.storage.models import NotificationLog
                db.add(NotificationLog(dedupe_key=idempotency_key, channel="feishu_app",
                                       status=row.status, error=row.last_error))
        return row
    except IntegrityError:
        # MariaDB REPEATABLE READ: the old snapshot may still miss the winner.
        # Only after a unique-key collision use a current read, never a gap lock on the initial miss.
        existing = db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.idempotency_key == idempotency_key
            ).with_for_update()
        )
        if existing:
            return existing
        raise
