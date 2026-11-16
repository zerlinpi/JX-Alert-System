"""Preserve frozen recipients and stop deliveries superseded by an owner group switch."""
from datetime import timezone

from jx_alert.notifications.routing import _load_routing, _routing_timestamp


def superseded_group_delivery(settings, payload, created_at):
    delivery = payload.get("delivery") or {}
    if payload.get("transport") != "app" or delivery.get("receive_id_type") != "chat_id":
        return False
    if delivery.get("role") == "private_question":
        return False
    routing = _load_routing(settings.feishu_routing_file)
    groups = routing.get("groups", {})
    alias = delivery.get("routing_alias")
    if alias is not None:
        group = groups.get(alias)
        return (not isinstance(group, dict) or group.get("chat_id") != delivery.get("receive_id")
                or group.get("notifications_changed_at") != delivery.get("routing_changed_at"))
    # Legacy payloads did not retain the alias. A retirement cutoff also prevents
    # old content from reviving if the owner later switches back to that chat.
    created = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
    return any(created <= _routing_timestamp(group["retired_chat_ids"][delivery.get("receive_id")])
               for group in groups.values()
               if delivery.get("receive_id") in group.get("retired_chat_ids", {}))
