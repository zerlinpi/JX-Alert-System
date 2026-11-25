"""The owner's Feishu routing file: report group, leads and verified rosters.

Sections used by this release (others written by earlier releases are kept
verbatim and ignored):
  groups.<alias>              chat_id, display_name, admin_open_ids,
                              verified_member_open_ids, notifications_changed_at
  company_executive_digest    总负责人: open_id, name, app_id, generation (the first person),
                              more_recipients[{open_id, name, generation}] (the others)
  ad_alert_subscribers        广告负责人: recipients[{open_id, name, generation}] (the first person),
                              more_recipients[...] (the others)
Releases before multiple leads read only the first person, so a rollback still reaches them.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,197}")
CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]{1,197}")


class ApplicationRoutingError(RuntimeError):
    """A safe configuration error code; never contains configuration values."""


def _open_ids(value, section):
    if not isinstance(value, list) or not all(isinstance(item, str) and OPEN_ID.fullmatch(item)
                                              for item in value):
        raise RuntimeError(f"Feishu routing section '{section}' must contain Feishu Open ID arrays")
    return list(dict.fromkeys(value))


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RuntimeError("Invalid Feishu routing change timestamp") from None


def validate_routing(data):
    if not isinstance(data, dict):
        raise RuntimeError("Feishu routing file root must be a JSON object")
    groups = data.get("groups", {})
    if not isinstance(groups, dict):
        raise RuntimeError("Feishu routing section 'groups' must be a JSON object")
    for alias, group in groups.items():
        if not isinstance(alias, str) or not alias or not isinstance(group, dict):
            raise RuntimeError("Each Feishu group must be a named JSON object")
        _open_ids(group.get("admin_open_ids", []), f"groups.{alias}.admin_open_ids")
        if "verified_member_open_ids" in group:
            _open_ids(group["verified_member_open_ids"], "verified_group_members")
        if "notifications_changed_at" in group:
            _timestamp(group["notifications_changed_at"])
    return data


def load_routing(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Feishu routing file cannot be read or parsed") from exc
    return validate_routing(data)


# Name kept for the bot intake and its tests.
_load_routing = load_routing


def report_group(settings, routing):
    """(alias, group) of the configured report group, or raise a safe code."""
    if getattr(settings, "feishu_transport", "app") != "app":
        raise ApplicationRoutingError("report_group_requires_application_transport")
    alias = getattr(settings, "feishu_daily_report_chat_id", "")
    groups = routing.get("groups", {}) if isinstance(routing, dict) else None
    if not isinstance(alias, str) or not alias or not isinstance(groups, dict):
        raise ApplicationRoutingError("missing_report_group_mapping")
    group = groups.get(alias)
    if not isinstance(group, dict):
        raise ApplicationRoutingError("missing_report_group_mapping")
    if not isinstance(group.get("chat_id"), str) or not CHAT_ID.fullmatch(group["chat_id"]):
        raise ApplicationRoutingError("missing_report_group_chat_id")
    return alias, group


def report_group_delivery(settings, routing, *, role="group"):
    """The explicitly configured report group, bound to its current generation."""
    alias, group = report_group(settings, routing)
    return {"role": role, "receive_id": group["chat_id"], "receive_id_type": "chat_id", "open_ids": [],
            "routing_alias": alias, "routing_changed_at": group.get("notifications_changed_at")}


def verified_members(routing, settings):
    """The last verified roster of the report group, or None when never verified."""
    try:
        _, group = report_group(settings, routing)
    except ApplicationRoutingError:
        return None
    roster = group.get("verified_member_open_ids")
    return None if roster is None else set(roster)
