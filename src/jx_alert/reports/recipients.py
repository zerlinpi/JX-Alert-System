"""Who receives each lane, resolved from the owner's routing file and store directory.

Every lane goes to the report group; for the review and the hourly monitor the
group message @-mentions the operators of the affected stores once they are
mapped. 总负责人 (company lead) also receives the daily report and 广告负责人
(ad lead) the review and the hourly monitor privately; private operator copies
(only their own stores) are optional. A recipient is re-verified before each send.
"""
import hashlib
import re

from jx_alert.notifications.routing import (
    OPEN_ID, ApplicationRoutingError, load_routing, report_group_delivery, verified_members,
)
from jx_alert.sparkx import stores as directory_module

COMPANY_LEAD = "company_executive_digest"
AD_LEAD = "ad_alert_subscribers"


def role(lane, kind):
    return f"jx_{lane}_{kind}"


def routing(settings):
    return load_routing(settings.feishu_routing_file)


def group_delivery(settings, routing_data, lane):
    try:
        return report_group_delivery(settings, routing_data, role=role(lane, "group"))
    except ApplicationRoutingError:
        return None


def operator_mentions(settings, routing_data, directory, pids):
    """{open_id: [profile ids]} of the mapped operators of these stores who are in the report group.

    Stores without an operator contribute nothing: their rows say 未映射运营 and the
    group message is still sent, just without an @ for them.
    """
    roster = verified_members(routing_data, settings)
    found = {}
    for key in sorted(pids, key=int):
        if not directory_module.monitored(directory, key):
            continue
        for person in directory_module.operators(directory, key):
            uid = person["open_id"]
            if roster is None or uid in roster:
                found.setdefault(uid, []).append(key)
    return found


def with_operator_mentions(delivery, mentions, names=None):
    """A group delivery that @-mentions exactly these operators (checked again before sending).

    ``names`` ({profile id: store name}) is frozen too, so the mention line can say
    which of their stores each operator is mentioned for.
    """
    names = names or {}
    keys = {key for values in mentions.values() for key in values}
    return {**delivery, "open_ids": list(mentions), "mentions": {uid: list(values) for uid, values in mentions.items()},
            "mention_names": {key: str(names[key])[:40] for key in sorted(keys, key=int) if names.get(key)}}


def current_mention_stores(settings, delivery):
    """[(open_id, [profile ids])] of the frozen @-mentions that are still valid now.

    An operator is kept for the stores that are still monitored and still theirs; a
    changed operator is simply not mentioned.
    """
    wanted, frozen = delivery.get("open_ids") or [], delivery.get("mentions") or {}
    if not wanted or not isinstance(frozen, dict):
        return []
    try:
        routing_data = routing(settings)
        directory = directory_module.load(settings)
    except (OSError, RuntimeError, ValueError, directory_module.StoreDirectoryError):
        return []
    roster = verified_members(routing_data, settings)
    keep = []
    for uid in dict.fromkeys(wanted):
        if not isinstance(uid, str) or not OPEN_ID.fullmatch(uid) or (roster is not None and uid not in roster):
            continue
        keys = frozen.get(uid) if isinstance(frozen.get(uid), list) else []
        valid = [key for key in keys if isinstance(key, str) and directory_module.monitored(directory, key)
                 and uid in {row["open_id"] for row in directory_module.operators(directory, key)}]
        if valid:
            keep.append((uid, valid))
    return keep


def current_mentions(settings, delivery):
    """The frozen @-mentions that are still valid now."""
    return [uid for uid, _ in current_mention_stores(settings, delivery)]


def mention_labels(delivery, entries, *, limit=4):
    """{open_id: "店铺A、店铺B"} for the mention line, from the store names frozen with the message."""
    names = delivery.get("mention_names") if isinstance(delivery.get("mention_names"), dict) else {}
    labels = {}
    for uid, keys in entries:
        shown = [names[key] for key in keys if isinstance(names.get(key), str) and names[key]]
        if shown:
            labels[uid] = "、".join(shown[:limit]) + (f" 等 {len(shown)} 家" if len(shown) > limit else "")
    return labels


def _app_identity(settings):
    from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError

    try:
        return FeishuAppClient.configured_app_identity(settings)
    except (FeishuAppError, OSError, TypeError, ValueError):
        return None


def company_lead(settings, routing_data):
    """总负责人, bound to the Feishu app that verified the choice."""
    binding = routing_data.get(COMPANY_LEAD) if isinstance(routing_data, dict) else None
    if not isinstance(binding, dict):
        return None
    uid, app_id, generation = binding.get("open_id"), binding.get("app_id"), binding.get("generation")
    if (not isinstance(uid, str) or not OPEN_ID.fullmatch(uid) or not isinstance(app_id, str)
            or not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation)):
        return None
    if _app_identity(settings) != hashlib.sha256(app_id.encode()).hexdigest():
        return None
    name = binding.get("name") if isinstance(binding.get("name"), str) else uid
    return {"open_id": uid, "name": name[:100], "generation": generation}


def ad_lead(routing_data):
    """广告负责人 (exactly one recipient), or None."""
    section = routing_data.get(AD_LEAD) if isinstance(routing_data, dict) else None
    rows = section.get("recipients") if isinstance(section, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if (not isinstance(row.get("open_id"), str) or not OPEN_ID.fullmatch(row["open_id"])
            or not isinstance(row.get("generation"), str) or not re.fullmatch(r"[0-9a-f]{32}", row["generation"])):
        return None
    name = row.get("name") if isinstance(row.get("name"), str) else row["open_id"]
    return {"open_id": row["open_id"], "name": name[:100], "generation": row["generation"]}


def lead(settings, routing_data, lane):
    person = company_lead(settings, routing_data) if lane == "daily" else ad_lead(routing_data)
    roster = verified_members(routing_data, settings)
    if person is None or (roster is not None and person["open_id"] not in roster):
        return None
    return person


def lead_delivery(settings, routing_data, lane):
    person = lead(settings, routing_data, lane)
    if person is None:
        return None
    return {"role": role(lane, "lead"), "receive_id_type": "open_id", "receive_id": person["open_id"],
            "open_ids": [], "generation": person["generation"], "name": person["name"]}


def operator_deliveries(settings, routing_data, directory, lane, pids, *, exclude=()):
    """One private delivery per operator, carrying exactly the profiles they own."""
    roster = verified_members(routing_data, settings)
    owners = {}
    for key in pids:
        for person in directory_module.operators(directory, key):
            uid = person["open_id"]
            if uid in exclude or (roster is not None and uid not in roster):
                continue
            owners.setdefault(uid, {"name": person["name"], "profiles": set()})["profiles"].add(key)
    return [{"role": role(lane, "operator"), "receive_id_type": "open_id", "receive_id": uid, "open_ids": [],
             "name": value["name"], "profiles": sorted(value["profiles"], key=int)}
            for uid, value in sorted(owners.items())]


def delivery_current(settings, lane, delivery):
    """None when the frozen recipient is still valid, otherwise a reason to keep the record."""
    try:
        routing_data = routing(settings)
    except (OSError, RuntimeError, ValueError):
        return "飞书路由配置无法核验，保留记录不补发"
    kind = (delivery or {}).get("role", "")
    if kind == role(lane, "group"):
        current = group_delivery(settings, routing_data, lane)
        if (current is None or current["receive_id"] != delivery.get("receive_id")
                or current.get("routing_changed_at") != delivery.get("routing_changed_at")):
            return "通知群已更换，保留记录不补发"
        return None
    if kind == role(lane, "lead"):
        current = lead_delivery(settings, routing_data, lane)
        if (current is None or current["receive_id"] != delivery.get("receive_id")
                or current["generation"] != delivery.get("generation")):
            return "负责人已变更，保留记录不补发"
        return None
    if kind == role(lane, "operator"):
        try:
            directory = directory_module.load(settings)
        except directory_module.StoreDirectoryError:
            return "店铺目录无法核验，保留记录不补发"
        roster = verified_members(routing_data, settings)
        uid, profiles = delivery.get("receive_id"), delivery.get("profiles")
        if (not isinstance(profiles, list) or not profiles or (roster is not None and uid not in roster)
                or any(not directory_module.monitored(directory, key)
                       or uid not in {row["open_id"] for row in directory_module.operators(directory, key)}
                       for key in profiles)):
            return "店铺对应的运营已变更，保留记录不补发"
        return None
    return "接收对象类型无效，保留记录"
