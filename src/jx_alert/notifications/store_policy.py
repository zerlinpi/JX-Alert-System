"""Apply store notification pauses to both new and previously queued work."""
from datetime import timezone

from jx_alert.store_controls import notification_states


def paused_delivery(settings, payload, created_at):
    delivery = payload.get("delivery") or {}
    role = delivery.get("role")
    if role == "group_question" and delivery.get("reply_to_message_id"):
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
