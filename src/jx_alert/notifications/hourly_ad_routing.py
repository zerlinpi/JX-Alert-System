"""Private hourly routing from one store's explicit, roster-verified assignments."""
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.notifications.routing import ApplicationRoutingError, _app_open_ids, _route_entry, _validate_routing_data


ROLE = "hourly_ad_store_recipient"


def scoped_routing_enabled(settings):
    return getattr(settings, "hourly_ad_store_routing_enabled", False) is True



def scoped_routing_file(path):
    return _validate_routing_data(json.loads(Path(path).read_text(encoding="utf-8"),
                                             object_pairs_hook=unique_json_fields))


def scoped_app(settings):
    """Return only the explicitly approved, currently configured app, never a secret."""
    expected = getattr(settings, "hourly_ad_store_routing_app_id", "")
    if not scoped_routing_enabled(settings) or not isinstance(expected, str) or not re.fullmatch(
            r"cli_[A-Za-z0-9]{1,128}", expected):
        return None
    try:
        configured = FeishuAppClient.configured_app_identity(settings)
    except (FeishuAppError, OSError, TypeError, ValueError):
        return None
    return expected if app_fingerprint(expected) == configured else None


def app_fingerprint(app_id):
    return hashlib.sha256(app_id.encode("utf-8")).hexdigest() if app_id else None


def _operator_name(value):
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()
    return value[:80] if value and not value.startswith("ou_") else None


def assignment(routing, sid, app_id):
    """Do not infer an operator from managers, subscribers, names or group admins."""
    store = _route_entry(routing.get("stores", {}), sid)
    alias = store.get("chat_id") or routing.get("default_chat_id")
    group = routing.get("groups", {}).get(alias, {})
    if (not app_id or group.get("hourly_verified_roster_app_id") != app_id
            or store.get("hourly_verified_operator_app_id") != app_id):
        return None
    try:
        owner_ids = _app_open_ids(store.get("open_ids", []))
        members = _app_open_ids(group.get("verified_member_open_ids", []))
        managers = _app_open_ids(store.get("manager_open_ids", []))
    except ApplicationRoutingError:
        return None
    chat = group.get("chat_id")
    if (not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat)
            or len(owner_ids) != 1 or owner_ids[0] not in members):
        return None
    owner = owner_ids[0]
    labels = store.get("owner_display_names")
    name = _operator_name(labels.get(owner)) if isinstance(labels, dict) else None
    if not name:
        return None
    generation = store.get("hourly_operator_generation")
    if generation is not None and (not isinstance(generation, str)
                                   or not re.fullmatch(r"[a-f0-9]{32}", generation)):
        return None
    # The store's own roster, not the report group or a global subscriber,
    # is the access boundary for its case details.
    recipients = {owner: "operator"}
    recipients.update({uid: "manager" for uid in managers if uid in members and uid != owner})
    fingerprints = {
        uid: hashlib.sha256(json.dumps(
            [app_id, sid, alias, chat, group.get("notifications_changed_at"), owner, name, generation, uid, role],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest() for uid, role in recipients.items()
    }
    return {"operator": owner, "operator_name": name, "recipients": recipients,
            "fingerprints": fingerprints}


def private_destination(uid):
    return {"role": ROLE, "receive_id_type": "open_id", "receive_id": uid, "open_ids": []}
