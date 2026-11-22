"""Destination planning and validated ownership routing; no network delivery."""
import json
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select

from jx_alert.product_identity import (
    ambiguous_legacy_product_key, exact_store_product, incident_seller_sku,
)
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import AdProduct, Product

def _validate_open_ids(value, section: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", item) for item in value
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
    return _validate_routing_data(data)


def _validate_routing_data(data: dict) -> dict:
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
        if "verified_member_open_ids" in group:
            _validate_open_ids(group["verified_member_open_ids"], "verified_group_members")
        if "notifications_changed_at" in group:
            _routing_timestamp(group["notifications_changed_at"])
        if "retired_chat_ids" in group:
            retired = group["retired_chat_ids"]
            if not isinstance(retired, dict):
                raise RuntimeError("Invalid retired Feishu destinations")
            for chat, stamp in retired.items():
                if not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat):
                    raise RuntimeError("Invalid retired Feishu destination")
                _routing_timestamp(stamp)

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

def _database_entity_open_ids(store_id: str | None, entity_id: str | None,
                             session_factory=None) -> list[str]:
    if not store_id or not entity_id:
        return []
    with (session_factory or SessionLocal)() as db:
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


class ApplicationRoutingError(RuntimeError):
    """A safe configuration error code; never contains configuration values."""


def _app_open_ids(value) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(uid, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid)
        for uid in value
    ):
        raise ApplicationRoutingError("invalid_open_id_mapping")
    return list(dict.fromkeys(value))


def _routing_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RuntimeError("Invalid Feishu routing change timestamp") from None


def group_member_mentions(group, open_ids):
    """Keep ownership mappings, but never mention a known nonmember of the new group."""
    ids = _app_open_ids(open_ids)
    if "verified_member_open_ids" not in group:
        return ids
    members = set(_app_open_ids(group["verified_member_open_ids"]))
    return [uid for uid in ids if uid in members]


def bind_group_delivery(routing, alias, delivery):
    """Freeze the destination generation; a later switch must not move old content."""
    group = routing["groups"][alias]
    return {**delivery, "routing_alias": alias,
            "routing_changed_at": group.get("notifications_changed_at")}


def report_group_delivery(settings, routing, *, role="group", reason=None):
    """Resolve only the explicitly configured report group; never invent recipients."""
    if getattr(settings, "feishu_transport", "webhook") != "app":
        raise ApplicationRoutingError("report_group_requires_application_transport")
    alias = getattr(settings, "feishu_daily_report_chat_id", "")
    groups = routing.get("groups", {}) if isinstance(routing, dict) else None
    if not isinstance(alias, str) or not alias or not isinstance(groups, dict):
        raise ApplicationRoutingError("missing_report_group_mapping")
    group = groups.get(alias)
    if not isinstance(group, dict):
        raise ApplicationRoutingError("missing_report_group_mapping")
    chat_id = group.get("chat_id")
    if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat_id):
        raise ApplicationRoutingError("missing_report_group_chat_id")
    # An absent role can use the approved group; a malformed mapping must still
    # be repaired. Validating this field does not create any mention.
    _app_open_ids(group.get("admin_open_ids", []))
    delivery = {"role": role, "receive_id": chat_id, "receive_id_type": "chat_id", "open_ids": []}
    if reason is not None:
        delivery.update(group_fallback=True, fallback_reasons=[reason])
    return bind_group_delivery(routing, alias, delivery)


def report_group_owner_mentions(routing, delivery, store_id):
    """Resolve this store's owners against the group receiving this message."""
    try:
        entry = _route_entry(routing.get('stores', {}), store_id)
        groups = routing.get('groups', {})
        group = groups.get(delivery.get('routing_alias'), {})
        chat_id = group.get('chat_id')
        if not isinstance(chat_id, str) or not chat_id.startswith('oc_') or chat_id != delivery.get('receive_id'):
            return []
        alias = entry.get('chat_id') or routing.get('default_chat_id')
        own_group = groups.get(alias, {})
        if 'verified_member_open_ids' not in group and own_group.get('chat_id') != chat_id:
            return []
        return group_member_mentions(group, _app_open_ids(entry.get('open_ids', [])))
    except (ApplicationRoutingError, TypeError, AttributeError):
        return []


def _application_route_entry(mapping, key, groups):
    """Distinguish an absent owner from a malformed responsible route."""
    if key not in mapping:
        return {}
    raw = mapping[key]
    if isinstance(raw, list):
        return {"open_ids": _app_open_ids(raw)}
    if not isinstance(raw, dict):
        raise ApplicationRoutingError("invalid_owner_mapping")
    _app_open_ids(raw.get("open_ids", []))
    alias = raw.get("chat_id")
    if alias is not None and (not isinstance(alias, str) or not alias or alias not in groups):
        raise ApplicationRoutingError("invalid_group_mapping")
    return raw


def _application_group_delivery(db, settings, routing, *, store_id, entity_id, metrics, scope):
    groups = routing.get("groups", {})
    if not isinstance(groups, dict) or any(
        not isinstance(routing.get(section, {}), dict) for section in ("stores", "entities")
    ):
        raise ApplicationRoutingError("invalid_group_mapping")
    sku = incident_seller_sku(store_id, entity_id, metrics, scope)
    entity_key = sku or str(metrics.get("seller_sku") or metrics.get("asin") or entity_id)
    stores, entities = routing.get("stores", {}), routing.get("entities", {})
    scoped_key = f"{store_id}:{entity_key}"
    store_route = _application_route_entry(stores, store_id, groups)
    if sku is not None:
        # A scoped entry (even empty or ambiguous) shadows the bare key. Raw
        # routing keys can label either a SKU or an ASIN, so only use an
        # unambiguous entry for either the destination or product mentions.
        product_route = {}
        if scoped_key in entities:
            if not ambiguous_legacy_product_key(db, store_id, sku, scoped=True):
                product_route = _application_route_entry(entities, scoped_key, groups)
        elif (sku in entities and ":" not in sku
              and not ambiguous_legacy_product_key(db, store_id, sku, scoped=False)):
            product_route = _application_route_entry(entities, sku, groups)
        chat_alias = (product_route.get("chat_id") or store_route.get("chat_id")
                      or routing.get("default_chat_id", "default"))
    else:
        _application_route_entry(entities, scoped_key, groups)
        _application_route_entry(entities, entity_key, groups)
        chat_alias = _route_chat_id(routing, store_id, entity_key)
    if not isinstance(chat_alias, str):
        raise ApplicationRoutingError("invalid_group_mapping")
    group = groups.get(chat_alias, {})
    if not isinstance(group, dict):
        raise ApplicationRoutingError("invalid_group_mapping")
    chat_id = group.get("chat_id")
    if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat_id):
        raise ApplicationRoutingError("missing_group_chat_id")
    try:
        default_ids = _app_open_ids(settings.feishu_at_ids())
    except ValueError:
        raise ApplicationRoutingError("invalid_default_mentions") from None
    admin_ids = default_ids + _app_open_ids(group.get("admin_open_ids", []))
    if sku is not None:
        # Keep store contacts and the exact SKU mapping, not contacts belonging
        # to its shared ASIN or to a different product now linked to an old ad.
        product = exact_store_product(db, store_id, sku)
        owner_ids = [*store_route.get("open_ids", []), *product_route.get("open_ids", [])]
        if product is not None:
            owner_ids.extend(_app_open_ids(product.owner_open_ids or []))
    else:
        owner_ids = _other_entity_owners(db, routing, groups, store_id, entity_id, entity_key, metrics, scope)
    owner_ids = list(dict.fromkeys(owner_ids))
    admin_ids = group_member_mentions(group, admin_ids)
    owner_ids = group_member_mentions(group, owner_ids)
    if not owner_ids:
        raise ApplicationRoutingError("missing_operations_owner")
    if not admin_ids:
        raise ApplicationRoutingError("missing_group_administrator")
    mentions = list(dict.fromkeys(admin_ids + owner_ids))
    return bind_group_delivery(routing, chat_alias, {
        "role": "group", "receive_id": chat_id, "receive_id_type": "chat_id", "open_ids": mentions})


def _other_entity_owners(db, routing, groups, store_id, entity_id, entity_key, metrics, scope):
    owner_ids = []
    keys = {str(value) for value in (entity_key, metrics.get("seller_sku"), metrics.get("asin")) if value}
    # Each advertised MSKU resolves independently. A shared ASIN must not pull in
    # another MSKU's owner when the actual advertised MSKU is already known.
    references = []
    if metrics.get("seller_sku") or metrics.get("asin"):
        references.append((metrics.get("seller_sku"), metrics.get("asin")))
    elif ":campaign:" not in scope:
        references.append((None, entity_key) if scope.endswith(":sku_asin") else (entity_key, None))
    # Only products actually linked to this store/type/campaign supply owners.
    if ":campaign:" in scope or ":product_ad:" in scope:
        query = select(AdProduct).where(AdProduct.store_id == store_id)
        campaign_type = scope.split(":")[-1]
        if campaign_type in {"sp", "sb", "sd"}:
            query = query.where(AdProduct.campaign_type == campaign_type)
        if ":campaign:" in scope:
            query = query.where(AdProduct.campaign_id == entity_id)
        else:
            query = query.where(AdProduct.ad_id == str(metrics.get("ad_id") or entity_id))
        for ad in db.scalars(query).all():
            keys.update(str(value) for value in (ad.seller_sku, ad.asin) if value)
            references.append((ad.seller_sku, ad.asin))
    for key in sorted(keys):
        # Validate only the responsible routes used by this event. An unrelated
        # store's invalid configuration must not suppress this store's alerts.
        for section, route_key in (
            ("stores", store_id), ("entities", f"{store_id}:{key}"), ("entities", key),
        ):
            entry = _application_route_entry(routing.get(section, {}), route_key, groups)
            owner_ids.extend(_app_open_ids(entry.get("open_ids", [])))
    if keys:
        products = db.scalars(select(Product).where(
            Product.store_id == store_id,
            or_(Product.seller_sku.in_(keys), Product.asin.in_(keys)),
        )).all()
        selected = {}
        for sku, asin in references:
            exact = [product for product in products if sku and product.seller_sku == str(sku)]
            # Generic entity IDs may themselves be ASINs. Explicit ASIN data is
            # preferred for fallback, and only used when the MSKU was unmapped.
            fallback_asin = str(asin or sku) if (asin or sku) else None
            matched = exact or [product for product in products if product.asin == fallback_asin]
            selected.update((product.id, product) for product in matched)
        for product in selected.values():
            owner_ids.extend(_app_open_ids(product.owner_open_ids or []))
    return owner_ids


def _application_management_deliveries(settings, routing, *, store_id, severity, status):
    management = routing.get("management", {})
    failed = not isinstance(management, dict)
    if failed:
        management = {}
    for field, allowed, current in (
        ("severities", {"P0", "P1", "P2", "P3"}, severity),
        ("statuses", {"NEW", "ESCALATED", "RECOVERED"}, status),
    ):
        values = management.get(field, sorted(allowed))
        if not isinstance(values, list) or any(
            not isinstance(value, str) or value not in allowed for value in values
        ):
            return [{"role": "management", "routing_failure": "invalid_management_filter"}]
        if current not in values:
            return []
    stores = routing.get("stores", {})
    if not isinstance(stores, dict):
        failed = True
        stores = {}
    sources = [
        management.get("open_ids", []),
        [item.strip() for item in getattr(settings, "feishu_manager_open_ids", "").split(",")
         if item.strip()],
        _route_entry(stores, store_id).get("manager_open_ids", []),
    ]
    manager_ids = []
    for source in sources:
        if not isinstance(source, list):
            failed = True
            continue
        for uid in source:
            if isinstance(uid, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid):
                manager_ids.append(uid)
            else:
                failed = True
    deliveries = [
        {"role": "management", "receive_id": uid, "receive_id_type": "open_id", "open_ids": []}
        for uid in dict.fromkeys(manager_ids)
    ]
    if failed or not manager_ids:
        deliveries.append({"role": "management", "routing_failure": "missing_or_invalid_manager_mapping",
                           "mapping_invalid": failed})
    return deliveries


def _merge_same_destinations(deliveries):
    """One event has one group delivery even when several missing roles use it."""
    merged, by_recipient = [], {}
    for item in deliveries:
        if item.get("routing_failure"):
            merged.append(item)
            continue
        identity = (item["receive_id_type"], item["receive_id"])
        if identity not in by_recipient:
            item = dict(item)
            by_recipient[identity] = item
            merged.append(item)
            continue
        existing = by_recipient[identity]
        existing["covered_roles"] = list(dict.fromkeys(
            existing.get("covered_roles", [existing["role"]]) + item.get("covered_roles", [item["role"]])))
        existing["open_ids"] = list(dict.fromkeys(existing.get("open_ids", []) + item.get("open_ids", [])))
        if existing.get("group_fallback") or item.get("group_fallback"):
            existing["group_fallback"] = True
            existing["fallback_reasons"] = list(dict.fromkeys(
                existing.get("fallback_reasons", []) + item.get("fallback_reasons", [])))
    return merged


def application_deliveries(db, settings, *, store_id: str, entity_id: str,
                           metrics: dict, scope: str, severity: str, status: str) -> list[dict]:
    """Plan valid destinations and independent, durable routing-failure roles.

    Missing roles may use the explicitly approved report group when enabled.
    Malformed configuration stays a failure, and valid manager messages remain
    independent. This planner never rearms persisted notifications.
    """
    return _application_plan(db, settings, store_id=store_id, entity_id=entity_id, metrics=metrics,
                             scope=scope, severity=severity, status=status)[0]


def _read_application_routing(settings):
    try:
        return json.loads(Path(settings.feishu_routing_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _application_plan(db, settings, *, store_id, entity_id, metrics, scope, severity, status, routing=None):
    if routing is None:
        routing = _read_application_routing(settings)
    routing_failed = not isinstance(routing, dict)
    if routing_failed:
        routing = {}
    try:
        if routing_failed:
            raise ApplicationRoutingError("routing_file_unavailable")
        group = _application_group_delivery(
            db, settings, routing, store_id=store_id, entity_id=entity_id, metrics=metrics, scope=scope,
        )
    except ApplicationRoutingError as exc:
        reason = str(exc)
        if (getattr(settings, "feishu_group_fallback_enabled", False) and not routing_failed
                and reason in {"missing_operations_owner", "missing_group_administrator"}):
            try:
                group = report_group_delivery(settings, routing, reason=reason)
            except ApplicationRoutingError as fallback_exc:
                group = {"role": "group", "routing_failure": str(fallback_exc)}
        else:
            group = {"role": "group", "routing_failure": reason}
    deliveries = [group]
    managers = _application_management_deliveries(
        settings, routing, store_id=store_id, severity=severity, status=status,
    )
    if routing_failed and not any(item.get("routing_failure") for item in managers):
        # Environment recipients can still receive the message, but the unread
        # file may contain more managers. Keep an explicit repairable failure.
        managers.append({"role": "management", "routing_failure": "routing_file_unavailable"})
    if (getattr(settings, "feishu_group_fallback_enabled", False) and not routing_failed
            and not group.get("routing_failure")):
        for index, item in enumerate(managers):
            if (item.get("routing_failure") == "missing_or_invalid_manager_mapping"
                    and not item.get("mapping_invalid")):
                try:
                    managers[index] = report_group_delivery(
                        settings, routing, role="management", reason=item["routing_failure"])
                except ApplicationRoutingError as exc:
                    managers[index] = {"role": "management", "routing_failure": str(exc)}
    deliveries.extend(managers)
    # Freeze the operations mode BEFORE merging. Management can independently
    # fall back to the same physical chat as a normally mapped operations group.
    mode = "fallback" if group.get("group_fallback") else "mapped"
    return _merge_same_destinations(deliveries), routing, mode


def enqueue_application_deliveries(db, settings, *, event_key: str, text: str,
                                   context: dict, roles: set[str] | None = None,
                                   card: dict | None = None, material_ad_alert: bool = False) -> dict:
    """Persist each destination or unresolved role once, in the event transaction."""
    from jx_alert.notifications.outbox import enqueue_notification
    from jx_alert.notifications.rule_scope import freeze_scope

    planned, routing, operations_mode = _application_plan(db, settings, **context)
    if roles is not None:
        planned = [item for item in planned if roles.intersection(item.get("covered_roles", [item["role"]]))]
    if material_ad_alert and roles is None:
        from jx_alert.notifications.recipient_policy import advertising_scope, advertising_subscriber_deliveries
        if advertising_scope(context.get('scope'), context.get('store_id')):
            # Subscribers are explicit extra recipients, not automatic managers.
            # Existing manager delivery to the same person already covers this
            # event; keep its original role and idempotency identity unchanged.
            known = {(r.get('receive_id_type'), r.get('receive_id')) for r in planned if not r.get('routing_failure')}
            planned.extend(r for r in advertising_subscriber_deliveries(settings)
                           if (r['receive_id_type'], r['receive_id']) not in known)
    outbox_ids = []
    unresolved = False
    for delivery in planned:
        failure = delivery.get("routing_failure")
        if failure:
            unresolved = True
            key = "app:routing:" + hashlib.sha256(
                f"{event_key}:{delivery['role']}".encode()
            ).hexdigest()
        else:
            # Keep the existing recipient key scheme across upgrades and repairs.
            key = "app:" + hashlib.sha256(
                f"{event_key}:{delivery['receive_id_type']}:{delivery['receive_id']}".encode()
            ).hexdigest()
        row = enqueue_notification(
            db, idempotency_key=key, text=text,
            store_id=context["store_id"], entity_id=context["entity_id"] or None,
            chat_id=delivery.get("receive_id", f"routing:{delivery['role']}"), delivery=delivery,
            card=card, material_ad_alert=material_ad_alert,
            rule_scope=None if failure else freeze_scope(
                db, context, delivery, material_ad_alert, routing, operations_mode),
            failed_route={"event_key": event_key, "routing_context": context,
                          "routing_role": delivery["role"]} if failure else None,
        )
        outbox_ids.append(row.id)
    db.flush()
    return {"outbox_ids": outbox_ids, "routing_failed": unresolved, "planned": len(planned)}


def repair_application_notification(db, settings, outbox_id: int) -> dict:
    """Re-plan one failed role after a mapping fix without resending other roles."""
    from jx_alert.notifications.outbox import NotificationOutbox
    from jx_alert.storage.models import NotificationLog

    row = db.get(NotificationOutbox, outbox_id)
    if row is None or row.status != "ROUTING_FAILED":
        raise ValueError("Only a failed application routing job can be repaired")
    payload = row.payload
    from jx_alert.notifications.quiet_policy import quiet_delivery, quiet_skip
    if quiet_delivery(settings) and payload.get("material_ad_alert") is not True:
        return quiet_skip()
    from jx_alert.notifications.store_policy import paused_delivery
    if paused_delivery(settings, payload, row.created_at):
        return {"status": "SKIPPED", "reason": "store_notifications_disabled", "outbox_ids": []}
    context = payload.get("routing_context")
    role, event_key = payload.get("routing_role"), payload.get("event_key")
    if not isinstance(context, dict) or role not in {"group", "management"} or not event_key:
        raise ValueError("Failed routing job has no valid repair context")
    result = enqueue_application_deliveries(
        db, settings, event_key=event_key, text=payload["text"], context=context, roles={role},
        card=payload.get("card"), material_ad_alert=payload.get("material_ad_alert") is True,
    )
    if not result["routing_failed"]:
        row.status = "SUPERSEDED"
        row.last_error = None
        db.add(NotificationLog(
            dedupe_key=row.idempotency_key, channel="feishu_app", status="SUPERSEDED",
        ))
    db.flush()
    return {"status": row.status, **result}


