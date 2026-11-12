"""Persistent Feishu application-bot and legacy-webhook outbox worker."""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from jx_alert.config import get_settings
from jx_alert.notifications.feishu import FeishuClient
from jx_alert.notifications.routing import (
    _load_routing, _routed_open_ids, _route_chat_id, _feishu_group_config,
    _database_entity_open_ids as _routing_database_ids,
)
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import NotificationLog

logger = logging.getLogger(__name__)


def _database_entity_open_ids(store_id, entity_id):
    return _routing_database_ids(store_id, entity_id, session_factory=SessionLocal)


def process_outbox_batch(batch_size: int = 20, max_attempts: int = 8,
                         outbox_ids: list[int] | None = None) -> dict:
    if outbox_ids is not None and any(type(x) is not int or x <= 0 for x in outbox_ids):
        raise ValueError("outbox_ids must contain positive integers")
    settings = get_settings()
    if settings.app_dry_run or not settings.feishu_enabled:
        return {"sent": 0, "skipped": True, "reason": "dry-run or Feishu disabled"}
    transport = getattr(settings, "feishu_transport", "webhook")
    routing = {} if transport == "app" else _load_routing(settings.feishu_routing_file)
    if transport == "webhook" and not routing.get("groups") and not settings.feishu_webhook_url:
        raise RuntimeError("FEISHU_WEBHOOK_URL is required when FEISHU_ENABLED=true")
    default_ids = [] if transport == "app" else settings.feishu_at_ids()
    default_chat_id = routing.get("default_chat_id", "default")
    client_cache = {}
    # Existing MariaDB DATETIME columns have second precision. Use the same
    # precision for the claim and its CAS predicate; attempts fence same-second claims.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    claim_time = now.replace(microsecond=0)
    # Cover the complete claimed batch, including token acquisition/refresh.
    lease_seconds = max(300, settings.feishu_timeout * 20 * 5 + 60)
    stale_before = now - timedelta(seconds=lease_seconds)

    # Recover claims left behind by a crashed worker. This is at-least-once delivery:
    # a crash after Feishu accepts a message but before DB commit can cause a duplicate.
    with SessionLocal() as db:
        db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.status == "SENDING",
                or_(
                    NotificationOutbox.claimed_at.is_(None),
                    NotificationOutbox.claimed_at < stale_before,
                ),
            )
            .values(status="RETRY", claimed_at=None, next_attempt_at=now)
        )
        db.commit()

        rows = db.scalars(
            select(NotificationOutbox)
            .where(NotificationOutbox.id.in_(outbox_ids) if outbox_ids is not None else True)
            .where(
                NotificationOutbox.status.in_(["PENDING", "RETRY"]),
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
            .limit(max(1, min(batch_size, 5)))
            .with_for_update(skip_locked=True)
        ).all()
        jobs = []
        for row in rows:
            attempts = row.attempts + 1
            claimed = db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == row.id,
                NotificationOutbox.status.in_(["PENDING", "RETRY"]),
                NotificationOutbox.next_attempt_at <= now,
            ).values(status="SENDING", claimed_at=claim_time,
                     attempts=attempts))
            if not claimed.rowcount:
                continue
            jobs.append((row.id, row.idempotency_key, dict(row.payload), attempts, row.chat_id))
        db.commit()

    sent = 0
    failed = 0
    for row_id, dedupe_key, payload, attempts, existing_chat_id in jobs:
        channel = "feishu_app" if payload.get("transport") == "app" else "feishu_webhook"
        message_id = None
        chat_id = (
            default_chat_id
            if existing_chat_id in (None, "", "default")
            else existing_chat_id
        )
        try:
            payload_ids = payload.get("open_ids") or []
            if not isinstance(payload_ids, list) or not all(
                isinstance(item, str) and item.startswith("ou_") for item in payload_ids
            ):
                raise ValueError("outbox open_ids must be a list of Feishu Open IDs")
            if payload.get("transport") == "app":
                from jx_alert.notifications.feishu_app import FeishuAppClient
                if "app" not in client_cache:
                    client_cache["app"] = FeishuAppClient.from_settings(settings)
                destination = payload["delivery"]
                chat_id = destination["receive_id"]
                response = client_cache["app"].send_text(
                    str(payload.get("text") or ""),
                    receive_id=chat_id,
                    receive_id_type=destination["receive_id_type"],
                    open_ids=destination.get("open_ids", []),
                    idempotency_key=dedupe_key,
                )
                message_id = response["message_id"]
            else:
                # Retain compatibility for persisted webhook notifications.
                chat_id = _send_webhook(
                    settings, routing, client_cache, payload, payload_ids, default_ids, chat_id
                )
            status = "DELIVERED"
            error = None
            delivered_at = datetime.now(timezone.utc).replace(tzinfo=None)
            sent += 1
        except Exception as exc:
            status = "DEAD" if attempts >= max_attempts else "RETRY"
            # Persist only the type; provider responses may echo credentials or content.
            error = f"Feishu delivery failed: {type(exc).__name__}"
            code = re.search(r"\bcode=(-?\d+)\b", str(exc))
            if code:
                error += f" code={code[1]}"
            delivered_at = None
            failed += 1
            logger.warning("Feishu delivery failed for outbox id=%s (%s)", row_id, type(exc).__name__)

        with SessionLocal() as db:
            values = dict(chat_id=chat_id, status=status, last_error=error,
                          claimed_at=None, delivered_at=delivered_at)
            if status == "RETRY":
                delay = min(30 * (2 ** max(attempts - 1, 0)), 3600)
                values["next_attempt_at"] = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)
            result = db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == row_id,
                NotificationOutbox.status == "SENDING",
                NotificationOutbox.claimed_at == claim_time,
                NotificationOutbox.attempts == attempts,
            ).values(**values))
            # An obsolete attempt must never overwrite a newer worker's result.
            attempt_status = status if result.rowcount == 1 else f"STALE_{status}"
            db.add(NotificationLog(
                dedupe_key=dedupe_key,
                channel=channel,
                status=attempt_status,
                message_id=message_id,
                error=error,
            ))
            db.commit()

    return {"sent": sent, "failed": failed, "claimed": len(jobs), "skipped": False}


def _send_webhook(settings, routing, client_cache, payload, payload_ids, default_ids, chat_id):
    if getattr(settings, "feishu_transport", "webhook") == "app":
        routing = _load_routing(settings.feishu_routing_file)
    if not payload.get("fixed_route"):
        chat_id = _route_chat_id(
            routing,
            payload.get("store_id"),
            payload.get("entity_id"),
            fallback=chat_id,
        )
    group = _feishu_group_config(routing, chat_id, settings)
    if chat_id not in client_cache:
        client_cache[chat_id] = FeishuClient(
            webhook_url=group["webhook_url"],
            sign_secret=group["sign_secret"],
            keyword=group["keyword"],
            timeout=settings.feishu_timeout,
        )
    open_ids = list(dict.fromkeys(
        default_ids
        + group["admin_open_ids"]
        + _routed_open_ids(routing, payload.get("store_id"), payload.get("entity_id"))
        + _database_entity_open_ids(payload.get("store_id"), payload.get("entity_id"))
        + payload_ids
    ))
    client_cache[chat_id].send_text(
        str(payload.get("text") or ""), open_ids=open_ids
    )
    return chat_id
