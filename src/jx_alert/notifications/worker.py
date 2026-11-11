"""At-least-once outbox worker for the Feishu custom-bot webhook."""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select, update

from jx_alert.config import get_settings
from jx_alert.notifications.feishu import FeishuClient
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import NotificationLog, Product

logger = logging.getLogger(__name__)


def _validate_open_ids(value, section: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.startswith("ou_") for item in value
    ):
        raise RuntimeError(
            f"Feishu routing section '{section}' must contain Feishu Open ID arrays"
        )
    return list(dict.fromkeys(value))


def _load_routing(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Feishu routing file cannot be read or parsed") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Feishu routing file root must be a JSON object")

    groups = data.get("groups", {})
    if not isinstance(groups, dict):
        raise RuntimeError("Feishu routing section 'groups' must be a JSON object")
    for group_id, group in groups.items():
        if not isinstance(group_id, str) or not group_id or not isinstance(group, dict):
            raise RuntimeError("Each Feishu group must be a named JSON object")
        for env_key in ("webhook_env", "sign_secret_env", "keyword_env"):
            env_name = group.get(env_key)
            if env_name is not None and (
                not isinstance(env_name, str)
                or not re.fullmatch(r"FEISHU_[A-Z0-9_]+", env_name)
            ):
                raise RuntimeError(f"Feishu group {group_id} has an invalid {env_key}")
        _validate_open_ids(group.get("admin_open_ids", []), f"groups.{group_id}.admin_open_ids")

    default_chat_id = data.get("default_chat_id", "default")
    if not isinstance(default_chat_id, str) or not default_chat_id:
        raise RuntimeError("default_chat_id must be a non-empty string")
    if groups and default_chat_id not in groups:
        raise RuntimeError("default_chat_id must reference a configured Feishu group")

    for section in ("stores", "entities"):
        mapping = data.get(section, {})
        if not isinstance(mapping, dict):
            raise RuntimeError(f"Feishu routing section '{section}' must be a JSON object")
        for key, route in mapping.items():
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"Feishu routing section '{section}' keys must be strings")
            if isinstance(route, list):
                _validate_open_ids(route, section)
                continue
            if not isinstance(route, dict):
                raise RuntimeError(
                    f"Feishu routing section '{section}' entries must be Open ID arrays or route objects"
                )
            _validate_open_ids(route.get("open_ids", []), f"{section}.{key}.open_ids")
            chat_id = route.get("chat_id")
            if chat_id is not None and (
                not isinstance(chat_id, str) or not chat_id
                or (groups and chat_id not in groups)
            ):
                raise RuntimeError(f"Feishu route {section}.{key} references an unknown chat_id")
    data["default_chat_id"] = default_chat_id
    return data


def _route_entry(mapping: dict, key: str | None) -> dict:
    if not key or not isinstance(mapping, dict):
        return {}
    entry = mapping.get(str(key), {})
    if isinstance(entry, list):
        return {"open_ids": entry}
    return entry if isinstance(entry, dict) else {}


def _routed_open_ids(routing: dict, store_id: str | None,
                     entity_id: str | None) -> list[str]:
    stores = routing.get("stores", {})
    entities = routing.get("entities", {})
    store_route = _route_entry(stores, store_id)
    entity_route = _route_entry(entities, f"{store_id}:{entity_id}")
    global_entity_route = _route_entry(entities, entity_id)
    ids = (
        list(store_route.get("open_ids", []))
        + list(entity_route.get("open_ids", []))
        + list(global_entity_route.get("open_ids", []))
    )
    if not all(isinstance(item, str) and item.startswith("ou_") for item in ids):
        raise RuntimeError("Feishu routing entries must contain valid Open IDs")
    return list(dict.fromkeys(ids))


def _route_chat_id(routing: dict, store_id: str | None,
                   entity_id: str | None, fallback: str = "default") -> str:
    stores = routing.get("stores", {})
    entities = routing.get("entities", {})
    entity_route = _route_entry(entities, f"{store_id}:{entity_id}")
    if not entity_route:
        entity_route = _route_entry(entities, entity_id)
    store_route = _route_entry(stores, store_id)
    return (
        entity_route.get("chat_id")
        or store_route.get("chat_id")
        or (routing.get("default_chat_id", "default") if fallback == "default" else fallback)
    )


def _feishu_group_config(routing: dict, chat_id: str, settings) -> dict:
    groups = routing.get("groups", {})
    if not groups:
        if chat_id != "default":
            raise RuntimeError(
                f"Feishu chat_id '{chat_id}' has no configured webhook group"
            )
        return {
            "webhook_url": settings.feishu_webhook_url,
            "sign_secret": settings.feishu_sign_secret,
            "keyword": settings.feishu_keyword,
            "admin_open_ids": [],
        }

    group = groups.get(chat_id)
    if group is None:
        raise RuntimeError(f"Feishu chat_id '{chat_id}' is not configured")

    def resolve(env_key: str, setting_name: str) -> str:
        env_name = group.get(env_key)
        if not env_name:
            return str(getattr(settings, setting_name, "") or "")
        if env_name == "FEISHU_WEBHOOK_URL":
            return str(getattr(settings, "feishu_webhook_url", "") or os.environ.get(env_name, ""))
        if env_name == "FEISHU_SIGN_SECRET":
            return str(getattr(settings, "feishu_sign_secret", "") or os.environ.get(env_name, ""))
        if env_name == "FEISHU_KEYWORD":
            return str(getattr(settings, "feishu_keyword", "") or os.environ.get(env_name, ""))
        return os.environ.get(env_name, "")

    webhook_url = resolve("webhook_env", "feishu_webhook_url")
    if not webhook_url:
        raise RuntimeError(
            f"Webhook environment variable for Feishu group '{chat_id}' is not configured"
        )
    return {
        "webhook_url": webhook_url,
        "sign_secret": resolve("sign_secret_env", "feishu_sign_secret"),
        "keyword": resolve("keyword_env", "feishu_keyword"),
        "admin_open_ids": _validate_open_ids(
            group.get("admin_open_ids", []), f"groups.{chat_id}.admin_open_ids"
        ),
    }

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
    default_chat_id = routing.get("default_chat_id", "default")
    client_cache = {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_before = now - timedelta(minutes=5)

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
            jobs.append((row.id, row.idempotency_key, dict(row.payload), row.attempts, row.chat_id))
        db.commit()

    sent = 0
    failed = 0
    for row_id, dedupe_key, payload, attempts, existing_chat_id in jobs:
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
                row.chat_id = chat_id
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
