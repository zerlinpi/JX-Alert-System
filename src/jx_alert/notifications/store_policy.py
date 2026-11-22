"""Apply store notification pauses to both new and previously queued work."""
from datetime import timezone
import json
from pathlib import Path

from jx_alert.store_controls import notification_states


def frozen_scope_current(settings, store_ids):
    """Explicit-scope reports cannot leave the scope that authorized their generation."""
    if not isinstance(store_ids, list) or not store_ids or not all(
            type(sid) is str and sid.isascii() and sid.isdecimal() for sid in store_ids):
        return False
    try:
        raw = json.loads(Path(settings.collection_scope_file).read_text(encoding="utf-8"))
        ids = raw.get("store_ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list) or not ids or any(
                type(sid) not in (int, str) or not str(sid).isascii() or not str(sid).isdecimal()
                or int(sid) <= 0 for sid in ids):
            return False
        return set(store_ids) <= {str(int(sid)) for sid in ids}
    except (AttributeError, OSError, TypeError, ValueError, RecursionError):
        return False


def paused_delivery(settings, payload, created_at):
    delivery = payload.get("delivery") or {}
    role = delivery.get("role")
    if role in {"group_question", "private_question"} and delivery.get("reply_to_message_id"):
        return False
    ids = payload.get("store_ids") or []
    if payload.get("store_id"):
        ids = [*ids, payload["store_id"]]
    # Legacy Chat Markdown reports had no store metadata. They can cover any
    # store, so any relevant pause blocks delivery of the unscoped report.
    states = notification_states(settings, ids or None) if ids or role == "chat_report" else {}
    created_at = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
    return any(not enabled or (cutoff is not None and created_at <= cutoff)
               for enabled, cutoff in states.values())
