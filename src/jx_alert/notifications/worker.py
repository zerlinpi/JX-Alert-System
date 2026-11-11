"""At-least-once outbox worker for the Feishu custom-bot webhook."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select, update

from jx_alert.config import get_settings
from jx_alert.notifications.feishu import FeishuClient
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import NotificationLog, Product

logger = logging.getLogger(__name__)


def _load_routing(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Feishu routing file cannot be read or parsed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Feishu routing file root must be a JSON object")
    for section in ("stores", "entities"):
        mapping = data.get(section, {})
        if not isinstance(mapping, dict):
            raise RuntimeError(f"Feishu routing section '{section}' must be a JSON object")
        for key, ids in mapping.items():
            if not isinstance(key, str) or not isinstance(ids, list) or not all(
                isinstance(item, str) and item.startswith("ou_") for item in ids
            ):
                raise RuntimeError(
                    f"Feishu routing section '{section}' must map keys to Open ID arrays"
                )
    return data


def _routed_open_ids(routing: dict, store_id: str | None,
                     entity_id: str | None) -> list[str]:
    ids: list[str] = []
    stores = routing.get("stores", {})
    entities = routing.get("entities", {})
    if store_id and isinstance(stores, dict):
        ids.extend(stores.get(str(store_id), []))
    if entity_id and isinstance(entities, dict):
        ids.extend(entities.get(f"{store_id}:{entity_id}", []))
        ids.extend(entities.get(str(entity_id), []))
    if not all(isinstance(item, str) and item for item in ids):
        raise RuntimeError("Feishu routing entries must be non-empty Open ID strings")
    return list(dict.fromkeys(ids))


def _database_entity_open_ids(store_id: str | None, entity_id: str | None) -> list[str]:
    if not store_id or not entity_id:
        return []
    with SessionLocal() as db:
        products = db.scalars(select(Product).where(
            Product.store_id == str(store_id),
            or_(
                Product.seller_sku == str(entity_id),
                Product.asin == str(entity_id),
            ),
        )).all()
        ids: list[str] = []
        for product in products:
            owners = product.owner_open_ids or []
            if not isinstance(owners, list) or not all(
                isinstance(item, str) and item.startswith("ou_") for item in owners
            ):
                raise RuntimeError(
                    "Product owner_open_ids must be a list of Feishu Open IDs"
                )
            ids.extend(owners)
        return list(dict.fromkeys(ids))


def process_outbox_batch(batch_size: int = 20, max_attempts: int = 8) -> dict:
    settings = get_settings()
    if settings.app_dry_run or not settings.feishu_enabled:
        return {"sent": 0, "skipped": True, "reason": "dry-run or Feishu disabled"}
    if not settings.feishu_webhook_url:
        raise RuntimeError("FEISHU_WEBHOOK_URL is required when FEISHU_ENABLED=true")

    routing = _load_routing(settings.feishu_routing_file)
    default_ids = settings.feishu_at_ids()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_before = now - timedelta(minutes=5)
    client = FeishuClient(
        webhook_url=settings.feishu_webhook_url,
        sign_secret=settings.feishu_sign_secret,
        keyword=settings.feishu_keyword,
        timeout=settings.feishu_timeout,
    )

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
            row.status = "SENDING"
            row.claimed_at = now
            row.attempts += 1
            jobs.append((row.id, row.idempotency_key, dict(row.payload), row.attempts))
        db.commit()

    sent = 0
    failed = 0
    for row_id, dedupe_key, payload, attempts in jobs:
        try:
            payload_ids = payload.get("open_ids") or []
            if not isinstance(payload_ids, list) or not all(
                isinstance(item, str) and item.startswith("ou_") for item in payload_ids
            ):
                raise ValueError("outbox open_ids must be a list of Feishu Open IDs")
            open_ids = list(dict.fromkeys(
                default_ids
                + _routed_open_ids(routing, payload.get("store_id"), payload.get("entity_id"))
                + _database_entity_open_ids(payload.get("store_id"), payload.get("entity_id"))
                + payload_ids
            ))
            client.send_text(str(payload.get("text") or ""), open_ids=open_ids)
            status = "DELIVERED"
            error = None
            delivered_at = datetime.now(timezone.utc).replace(tzinfo=None)
            sent += 1
        except Exception as exc:  # sanitized client errors never include the webhook URL
            status = "DEAD" if attempts >= max_attempts else "RETRY"
            error = f"{type(exc).__name__}: {str(exc)[:1000]}"
            delivered_at = None
            failed += 1
            logger.warning("Feishu delivery failed for outbox id=%s (%s)", row_id, type(exc).__name__)

        with SessionLocal() as db:
            row = db.get(NotificationOutbox, row_id)
            if row:
                row.status = status
                row.last_error = error
                row.claimed_at = None
                row.delivered_at = delivered_at
                if status == "RETRY":
                    delay = min(30 * (2 ** max(attempts - 1, 0)), 3600)
                    row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)
            db.add(NotificationLog(
                dedupe_key=dedupe_key,
                channel="feishu_webhook",
                status=status,
                message_id=None,
                error=error,
            ))
            db.commit()

    return {"sent": sent, "failed": failed, "claimed": len(jobs), "skipped": False}
