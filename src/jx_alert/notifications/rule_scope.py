"""Insert-only provenance and pre-POST authorization for generic App rule alerts.

The key shape identifies a producer family, never a store, SKU or campaign.
Dedicated hourly, overnight, reviewed Chat and scheduled notifications have
different namespaces and different recipient contracts.
"""
from datetime import timedelta, timezone
import re

from sqlalchemy import select

from jx_alert.notifications.recipient_policy import ROLE, subscriber_delivery_blocked
from jx_alert.notifications.routing import (
    _application_management_deliveries, _application_plan, _load_routing, _read_application_routing,
)
from jx_alert.product_identity import exact_store_product, incident_seller_sku
from jx_alert.storage.models import AdProduct, Campaign, Store

BLOCKED = "原告警的店铺、对象、接收人或群范围无法核验，未发送，保留记录不补发"


def generic_key(key):
    return re.fullmatch(r"app:[0-9a-f]{64}", key) is not None


def _id(value):
    return isinstance(value, str) and bool(value.strip())


def freeze_scope(db, ctx, delivery, material, routing, operations_mode):
    """Only typed evaluator fields establish identity; never inspect the text/card."""
    sid, entity, scope = ctx["store_id"], ctx["entity_id"], ctx["scope"]
    metrics = ctx["metrics"]
    sku = incident_seller_sku(sid, entity, metrics, scope)
    group = delivery["receive_id_type"] == "chat_id"
    if sku is None and (scope == f"{sid}:sales:sku_msku" or (
            scope in (f"{sid}:product_ad:sp", f"{sid}:product_ad:sd")
            and metrics.get("seller_sku") not in (None, ""))):
        return {"v": 1, "kind": "unproven"}  # A broken typed identity is not an unrelated rule.
    if not material and sku is None:
        return {"v": 1, "kind": "other"}

    proof = {"v": 1, "store": sid, "subject": entity, "severity": ctx["severity"], "status": ctx["status"]}
    if scope == f"{sid}:sales:sku_msku" and sku is not None and not material:
        proof["kind"] = "sales_sku"
    else:
        for family, types in (("campaign", ("sp", "sb", "sd")), ("product_ad", ("sp", "sd"))):
            for ad_type in types:
                if scope in (f"{sid}:{family}:{ad_type}", f"{family}_{ad_type}"):
                    proof.update(kind=family, ad_type=ad_type)
                    if family == "campaign":
                        # In a typed campaign event the subject IS the campaign id.
                        proof["campaign"] = entity
                    else:
                        proof.update(ad=metrics.get("ad_id"), sku=sku)
                        if material:
                            proof["campaign"] = metrics.get("campaign_id")
        if "kind" not in proof:
            return {"v": 1, "kind": "unproven"}
    if sku is not None and group:
        product = exact_store_product(db, sid, sku)
        original_group = routing.get("groups", {}).get(delivery["routing_alias"], {})
        proof["group"] = {
            "mode": operations_mode if delivery["role"] == "group" else "management_fallback",
            "had_roster": "verified_member_open_ids" in original_group,
            # Explicit routes/approved unmentioned fallback work without a
            # catalog row. A row which once supplied the identity cannot vanish.
            "catalog_id": product.id if product is not None else None,
        }
    return proof


def valid_shape(payload):
    p, d = payload.get("rule_scope"), payload.get("delivery")
    material = payload.get("material_ad_alert", False)
    if (payload.get("transport") != "app" or not isinstance(d, dict)
            or d.get("reply_to_message_id") or type(material) is not bool):
        return False
    group = d.get("receive_id_type") == "chat_id" and d.get("role") in ("group", "management")
    private = d.get("receive_id_type") == "open_id" and d.get("role") in ("management", ROLE)
    if not (group or private):
        return False
    # Only the independent non-material private manager lane retains legacy behavior.
    if p is None:
        return "rule_scope" not in payload and private and d["role"] == "management" and not material
    if not isinstance(p, dict) or type(p.get("v")) is not int or p["v"] != 1:
        return False
    kind = p.get("kind")
    if kind == "other":
        return set(p) == {"v", "kind"} and not material
    if (not _id(p.get("store")) or p["store"] != payload.get("store_id") or payload.get("store_ids")
            or not _id(p.get("subject")) or p["subject"] != payload.get("entity_id")
            or p.get("severity") not in ("P0", "P1", "P2", "P3")
            or p.get("status") not in ("NEW", "ESCALATED", "ONGOING", "RECOVERED")):
        return False
    fields = {"v", "kind", "store", "subject", "severity", "status"}
    if kind == "sales_sku":
        if material:
            return False
        exact = True
    elif kind == "product_ad":
        fields |= {"ad_type", "ad", "sku"}
        if p.get("ad_type") not in ("sp", "sd") or not _id(p.get("ad")):
            return False
        exact = p.get("sku") == p["subject"]
        if (not exact and p.get("sku") is not None) or (not material and not exact):
            return False
    elif kind == "campaign" and material and p.get("ad_type") in ("sp", "sb", "sd"):
        fields.add("ad_type")
        exact = False
        if p.get("campaign") != p["subject"]:
            return False
    else:
        return False
    if material:
        fields.add("campaign")
        if not _id(p.get("campaign")):
            return False
    if group and exact:
        fields.add("group")
        binding = p.get("group")
        if (not isinstance(binding, dict) or set(binding) != {"mode", "catalog_id", "had_roster"}
                or binding["mode"] not in (("mapped", "fallback") if d["role"] == "group" else ("management_fallback",))
                or type(binding["had_roster"]) is not bool or binding["catalog_id"] is not None
                and (type(binding["catalog_id"]) is not int or binding["catalog_id"] <= 0)):
            return False
    return set(p) == fields


def _expiry(row, now):
    stamp = row.updated_at if row is not None else None
    if stamp is not None and stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    if (stamp is not None and (row.state or "").casefold() == "enabled"
            and timedelta() <= now - stamp <= timedelta(minutes=90)):
        return stamp + timedelta(minutes=90)
    return None


def material_deadline(db, payload, now):
    """Return the original rows' earliest expiry; never look up a campaign by SKU."""
    if not valid_shape(payload):
        return None
    p = payload["rule_scope"]
    sid, kind, campaign_id = p["store"], p["ad_type"], p["campaign"]
    campaign = db.scalar(select(Campaign).where(Campaign.store_id == sid,
        Campaign.campaign_type == kind, Campaign.campaign_id == campaign_id))
    deadline = _expiry(campaign, now)
    if (deadline is None
            or (campaign.store_id, campaign.campaign_type, campaign.campaign_id) != (sid, kind, campaign_id)):
        return None
    if p["kind"] == "product_ad":
        ad = db.scalar(select(AdProduct).where(AdProduct.store_id == sid,
                AdProduct.campaign_type == kind, AdProduct.ad_id == p["ad"]))
        ad_expiry = _expiry(ad, now)
        if (ad_expiry is None or (ad.store_id, ad.campaign_type, ad.campaign_id, ad.ad_id) !=
                (sid, kind, campaign_id, p["ad"]) or (ad.seller_sku or None) != p["sku"]
                or (ad.seller_sku or ad.asin or ad.ad_id) != p["subject"]):
            return None
        deadline = min(deadline, ad_expiry)
    return deadline


def guarded(payload):
    """Malformed and legacy group/material rows take the conservative hold path."""
    return not valid_shape(payload) or (payload.get("rule_scope") or {}).get("kind") not in (None, "other")


def route_current(db, settings, payload):
    """Use a fresh session and the current local role/roster snapshot; no remote reads."""
    if not valid_shape(payload):
        return False
    p, d = payload["rule_scope"], payload["delivery"]
    store = db.get(Store, p["store"])
    if store is None or store.sid != p["store"] or store.status != 1:
        return False
    material = payload.get("material_ad_alert") is True
    if material and store.has_ads_setting != 1:
        return False
    group = d["receive_id_type"] == "chat_id"
    if not group:
        if d["role"] == ROLE:
            return not subscriber_delivery_blocked(settings, payload)
        # Managers are independent. A removed SKU owner or unrelated invalid
        # group entry does not revoke an explicitly authorized private manager.
        routing = _read_application_routing(settings)
        if not isinstance(routing, dict) or d.get("open_ids") != []:
            return False
        return any(r.get("receive_id") == d.get("receive_id") and r.get("receive_id_type") == "open_id"
                   for r in _application_management_deliveries(settings, routing,
                       store_id=p["store"], severity=p["severity"], status=p["status"]))
    routing = _load_routing(settings.feishu_routing_file)
    group_cfg = routing.get("groups", {}).get(d.get("routing_alias"))
    if (not isinstance(group_cfg, dict) or group_cfg.get("chat_id") != d.get("receive_id")
            or group_cfg.get("notifications_changed_at") != d.get("routing_changed_at")):
        return False
    binding = p.get("group")
    if binding:
        if binding["had_roster"] and "verified_member_open_ids" not in group_cfg:
            return False
        if binding["catalog_id"] is not None:
            product = exact_store_product(db, p["store"], p["subject"])
            if product is None or product.id != binding["catalog_id"]:
                return False
    else:
        # Non-MSKU material events retain their existing group routing policy;
        # the new guarantee here is about the exact ad/campaign, not SKU owners.
        return True
    scope = "sales:sku_msku" if p["kind"] == "sales_sku" else f"{p['kind']}:{p['ad_type']}"
    fresh, _, ops_mode = _application_plan(db, settings, routing=routing, store_id=p["store"],
        scope=f"{p['store']}:{scope}", entity_id=p["subject"], severity=p["severity"], status=p["status"],
        metrics={"seller_sku": p.get("sku"), "ad_id": p.get("ad"), "campaign_id": p.get("campaign")})
    for candidate in fresh:
        if candidate.get("routing_failure") or any(candidate.get(k) != d.get(k) for k in (
                "role", "receive_id_type", "receive_id", "routing_alias", "routing_changed_at")):
            continue
        mode = ops_mode if candidate["role"] == "group" else "management_fallback"
        # Keep the rendered body frozen. A changed role/mentions set requires a
        # new event, not a rewritten body, new destination or automatic retry.
        return (mode == binding["mode"] and set(candidate["open_ids"]) == set(d["open_ids"])
                and set(candidate.get("covered_roles", [candidate["role"]])) ==
                    set(d.get("covered_roles", [d["role"]])))
    return False
