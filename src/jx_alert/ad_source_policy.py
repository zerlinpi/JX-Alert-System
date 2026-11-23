"""Prevent legacy Lingxing alerts from crossing a manual advertising-source switch."""

from datetime import datetime, timezone

from jx_alert.ad_source_controls import source_snapshot


BLOCKED = "广告数据源已切换或无法核验，旧告警保留记录、不补发"


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("Missing advertising source time")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def lingxing_ad_allowed(settings, sid, *, evidence_at=None):
    try:
        selection = source_snapshot(settings, str(sid))
        if selection.get("source") != "lingxing":
            return False
        changed = selection.get("selected_at")
        return changed is None or (evidence_at is None or _instant(evidence_at) > _instant(changed))
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        return False


def legacy_ad_payload(payload):
    return (payload.get("hourly_ad_monitor") is True
            or payload.get("morning_message_kind") == "ad_exception"
            or payload.get("material_ad_alert") is True)


def legacy_payload_allowed(settings, payload, created_at):
    ids = {str(sid) for sid in payload.get("store_ids") or []}
    if payload.get("store_id"):
        ids.add(str(payload["store_id"]))
    # Hourly summaries can be regenerated per campaign before sending. This
    # predicate checks only the *actual* included rows, not excluded stores.
    if "hourly_items" in payload:
        items = payload["hourly_items"]
        return isinstance(items, list) and bool(items) and all(
            isinstance(item, dict) and item.get("store_id") and item.get("detected_at")
            and lingxing_ad_allowed(settings, item["store_id"], evidence_at=item["detected_at"])
            for item in items
        )
    # Other pages have been frozen and must not be rendered for another source.
    return bool(ids) and all(lingxing_ad_allowed(settings, sid, evidence_at=created_at) for sid in ids)
