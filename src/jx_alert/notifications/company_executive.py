"""Dedicated opt-in 08:30 Beijing company digest; no shared or fallback receiver."""

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jx_alert.notifications.cards import card, card_fits
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.operators import UNMAPPED_OPERATOR, store_operator_labels
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.notifications.store_policy import frozen_scope_current, paused_delivery
from jx_alert.reporting.company_executive import (
    BEIJING, CLOSES, LATEST, MARKETPLACE, METRICS, OPENS, company_snapshot,
)
from jx_alert.store_controls import collection_states, notification_states

PREFIX = "scheduled-daily:company-executive:"
ROLE = "company_executive_digest"
BLOCKED = "公司日报私聊权限或店铺范围已变更，保留旧记录，不迁移或补发"
SECTION = "company_executive_digest"
_REQUIRED = {"open_id", "app_id", "generation", "verified_at"}
_OPTIONAL = {"name", "store_ids", "updated_at"}
_LABELS = {"sales": "销售额", "orders": "订单量¹", "spend": "广告花费（SP/SB/SD）",
           "acos": "ACOS", "tacos": "TACOS", "cvr": "CVR（仅 SP 点击转化率）",
           "ppc": "PPC（CPC，单次点击花费）"}
_NOTE_LIMIT = 900


def is_company_digest(key, payload):
    delivery = payload.get("delivery")
    return (key.startswith(PREFIX)
            or isinstance(delivery, dict) and delivery.get("role") == ROLE)


def delivery_window(key, payload, clock):
    """Use one canonical key per local day, with an exclusive trailing deadline."""
    local = clock.astimezone(BEIJING)
    try:
        day = date.fromisoformat(payload["scheduled_delivery_date"])
        if key != PREFIX + day.isoformat() or payload.get("scheduled_timezone") != BEIJING.key:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return "EXPIRED", None
    due = datetime.combine(day, OPENS, BEIJING)
    if local >= datetime.combine(day, CLOSES, BEIJING):
        return "EXPIRED", None
    if local < due:
        return "WAITING", due.astimezone(timezone.utc).replace(tzinfo=None)
    return "READY", None


def _scope(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 1000
            or any(type(value) not in (str, int)
                   or not re.fullmatch(r"[1-9][0-9]{0,29}", str(value)) for value in values)):
        raise ValueError("Company digest requires explicit store IDs")
    ids = sorted(set(map(str, values)))
    if len(ids) != len(values):
        raise ValueError("Duplicate company digest store IDs")
    return ids


def _json_file(path):
    config = Path(path)
    if not config.is_file() or config.stat().st_size > 1024 * 1024:
        raise ValueError("Company digest requires readable local configuration")
    value = json.loads(config.read_bytes(), object_pairs_hook=unique_json_fields)
    if not isinstance(value, dict):
        raise ValueError("Invalid company digest local configuration")
    return value


def _binding(settings):
    """The owner-approved private recipient, or None. Malformed content fails closed."""
    binding = _json_file(settings.feishu_routing_file).get(SECTION)
    if binding is None:
        return None
    if not isinstance(binding, dict) or not _REQUIRED <= set(binding) <= _REQUIRED | _OPTIONAL:
        raise ValueError("Invalid company digest recipient")
    return binding


def _active_scope(settings, binding=None):
    """Company scope: configured stores whose collection and notifications are both on.

    A binding may carry an explicit owner-approved ``store_ids`` subset; it can
    only narrow the scope. Paused stores are counted, never silently included.
    """
    configured = _scope(_json_file(settings.collection_scope_file).get("store_ids"))
    collecting = collection_states(settings, configured)
    notifying = notification_states(settings, configured)
    active = [sid for sid in configured if collecting[sid] and notifying[sid][0]]
    if binding is not None and "store_ids" in binding:
        approved = set(_scope(binding["store_ids"]))
        active = [sid for sid in active if sid in approved]
    return active, len(configured) - len(active)


def _store_basis(settings, binding=None):
    ids, excluded = _active_scope(settings, binding)
    zones = _json_file(settings.store_timezones_file).get("stores")
    if not isinstance(zones, dict):
        raise ValueError("Company digest timezone configuration unavailable")
    return ids, {sid: (zones.get(sid) or {}).get("timezone") for sid in ids
                 if isinstance(zones.get(sid), dict)}, excluded


def operator_labels(settings, store_ids):
    """Resolve each store to its mapped operator names; never guess an unnamed person."""
    try:
        routing = _json_file(settings.feishu_routing_file)
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return store_operator_labels(routing, store_ids)


def preview_company_digest(db, settings, *, now):
    """Pure preview using local configured scope; neither recipient config nor outbox needed."""
    try:
        binding = _binding(settings)
    except (OSError, ValueError, TypeError, RecursionError):
        binding = None
    ids, zones, excluded = _store_basis(settings, binding)
    result = company_snapshot(db, ids, zones, now=now, operators=operator_labels(settings, ids),
                              excluded_store_count=excluded)
    return result, company_card(result)


def _route(settings, now):
    if (not getattr(settings, "feishu_company_executive_digest_enabled", False)
            or not settings.scheduler_enabled or settings.app_dry_run or not settings.feishu_enabled
            or settings.feishu_transport != "app"):
        return None
    try:
        raw_start = settings.feishu_company_executive_digest_start_date
        start = date.fromisoformat(raw_start)
        if raw_start != start.isoformat() or now.astimezone(BEIJING).date() < start:
            return None
        binding = _binding(settings)
        if binding is None:
            return None
        opened = binding["open_id"]
        app_id = binding["app_id"]
        generation = binding["generation"]
        stamp = datetime.fromisoformat(binding["verified_at"])
        if (not isinstance(opened, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,150}", opened)
                or not isinstance(app_id, str) or not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,150}", app_id)
                or not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation)
                or stamp.tzinfo is None or stamp.utcoffset() is None or stamp > now
                or "name" in binding and not isinstance(binding["name"], str)):
            return None
        if "store_ids" in binding:
            _scope(binding["store_ids"])
        identity = hashlib.sha256(app_id.encode("utf-8")).hexdigest()
        if FeishuAppClient.configured_app_identity(settings) != identity:
            return None
        return dict(role=ROLE, receive_id_type="open_id", receive_id=opened, open_ids=[],
                    generation=generation, verified_at=stamp.astimezone(timezone.utc).isoformat(),
                    app_identity_sha256=identity)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, OverflowError, RecursionError):
        return None


def delivery_current(db, settings, key, payload, created_at, clock, chat_id):
    """Fail closed across route changes, store scope/pauses, old items and mutations."""
    instant = clock.replace(tzinfo=timezone.utc) if clock.tzinfo is None else clock
    if delivery_window(key, payload, instant)[0] != "READY":
        return False
    if (payload.get("transport") != "app" or payload.get("fixed_route") is not True
            or payload.get("open_ids") != [] or payload.get("store_id") is not None
            or payload.get("entity_id") is not None or not isinstance(payload.get("card"), dict)):
        return False
    try:
        ids = payload.get("store_ids")
        current = _route(settings, instant)
        if (current is None or current != payload.get("delivery") or current["receive_id"] != chat_id
                or not isinstance(ids, list) or not ids or _scope(ids) != ids
                or not frozen_scope_current(settings, ids)):
            return False
        created = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
        return (datetime.fromisoformat(current["verified_at"]) <= created <= instant
                and not paused_delivery(settings, payload, created_at))
    except (OSError, TypeError, ValueError, RuntimeError, RecursionError):
        return False


def _metric_text(name, value, code):
    if value is None:
        return "—"
    if name == "orders":
        return f"{value:,}"
    if name in {"acos", "tacos", "cvr"}:
        return f"{value:.2%}"
    return f"{value:,.2f} {code}" if code else f"{value:,.2f}"


def _join_limited(parts, limit=_NOTE_LIMIT):
    shown, size = [], 0
    for part in parts:
        if size + len(part) > limit and shown:
            break
        shown.append(part)
        size += len(part) + 1
    text = "；".join(shown)
    return text + (f"；等共 {len(parts)} 家" if len(shown) < len(parts) else "")


def company_card(snapshot, *, include_operators=True, gap_limit=_NOTE_LIMIT):
    order_label = "订单量¹"
    across = snapshot["company_orders"]
    total = snapshot["store_count"]
    excluded = snapshot.get("excluded_store_count") or 0
    scope_line = f"北京时间 08:30 · 统计店铺 {total} 家（采集与通知均开启）"
    if excluded:
        scope_line += f"，另有 {excluded} 家未纳入统计"
    subtitle = (scope_line + "\n"
                f"最近已完结经营日：{snapshot['date']}（美西） · 成熟归因日：{snapshot['mature_date']}（美西）\n"
                f"全公司{order_label}：" + (f"{across:,}（覆盖 {snapshot['orders_covered']}/{total} 店）"
                                          if across is not None else f"暂无完整依据（0/{total} 店）"))
    latest, settled = [], []
    for code, data in snapshot["currencies"].items():
        for metric in METRICS:
            rows = latest if metric in LATEST else settled
            rows.append((code, _LABELS[metric], _metric_text(metric, data["values"][metric], code),
                         f"{data['covered'][metric]}/{data['store_count']} 店"))
    sections = [
        (f"全公司 · 最近已完结经营日 {snapshot['date']}（美西）", ["币种", "指标", "数值", "覆盖"], latest),
        (f"全公司 · 成熟归因日 {snapshot['mature_date']}（美西）", ["币种", "指标", "数值", "覆盖"], settled),
    ]
    if include_operators and snapshot.get("operators"):
        current, mature = [], []
        for row in snapshot["operators"]:
            code, values, covered = row["currency"], row["values"], row["covered"]
            who = row["operator"] + (f" · {code}" if code and len(snapshot["currencies"]) > 1 else "")
            stores = f"{row['store_count']}/{row['store_total']}"
            current.append((who, stores, _metric_text("sales", values["sales"], code),
                            _metric_text("orders", values["orders"], code),
                            _metric_text("spend", values["spend"], code),
                            _metric_text("tacos", values["tacos"], code),
                            _metric_text("ppc", values["ppc"], code),
                            f"销售 {covered['sales']} · 广告 {covered['spend']}"))
            mature.append((who, _metric_text("acos", values["acos"], code),
                           _metric_text("cvr", values["cvr"], code),
                           f"ACOS {covered['acos']} · CVR {covered['cvr']}"))
        sections.append((f"按运营 · {snapshot['date']}（美西）",
                         ["运营", "店铺²", "销售额", "订单量¹", "广告花费", "TACOS", "PPC", "覆盖店数"], current))
        sections.append((f"按运营 · 成熟归因日 {snapshot['mature_date']}（美西）",
                         ["运营", "ACOS", "CVR", "覆盖店数"], mature))
    notes = []
    if snapshot.get("issues"):
        notes.append("未计入：" + _join_limited(
            [f"{row['name']}（{row['issue']}）" for row in snapshot["issues"]], gap_limit))
    if snapshot.get("gaps"):
        notes.append("部分指标未计入：" + _join_limited(
            [f"{row['name']}（{'、'.join(row['gaps'])}）" for row in snapshot["gaps"]], gap_limit))
    notes += [
        "合计和比率只含具备完整依据的店铺；未覆盖店铺不按 0 计，— 表示没有可用依据或分母为零。",
        "¹ 订单量为按 ASIN 商品汇总，非唯一订单去重；不同币种不换算、不合计。",
        "TACOS=同日 SP/SB/SD 广告花费÷店铺销售额；PPC 按 CPC 算式显示，为混合花费÷点击，可能含按展示付费。",
        f"最近经营日的 ACOS 和 CVR 不显示：归因尚未成熟。成熟页仅指 {snapshot['mature_date']}，"
        "ACOS=SP/SB/SD 花费÷同日归因销售额；CVR=SP 点击归因订单÷同日 SP 点击，均先汇总再算。",
    ]
    if include_operators and snapshot.get("operators"):
        notes.append(f"² 店铺列为“有完整销售依据的店铺/该运营负责的统计店铺”；运营来自运营台负责人映射，"
                     f"未映射的店铺归入“{UNMAPPED_OPERATOR}”。")
    return card("公司经营与广告快报", subtitle=subtitle, sections=sections, notes=notes)


def fitted_company_card(snapshot):
    """Prefer the full card; shorten store lists, then drop the operator tables to fit."""
    for options in ({}, {"gap_limit": 300}, {"include_operators": False, "gap_limit": 300}):
        value = company_card(snapshot, **options)
        if card_fits(value):
            return value
    return None


def queue_company_digest(db, settings, *, now):
    """Freeze at most one message per Beijing day under the unique outbox key."""
    if not getattr(settings, "feishu_company_executive_digest_enabled", False):
        return "DISABLED"
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Company digest requires a timezone-aware clock")
    local = now.astimezone(BEIJING)
    if not OPENS <= local.time() < CLOSES:
        return "WAITING"
    key = PREFIX + local.date().isoformat()
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)) is not None:
        return "ALREADY_QUEUED"
    target = _route(settings, now)
    if target is None:
        return "ROUTE_UNVERIFIED"
    ids, zones, excluded = _store_basis(settings, _binding(settings))
    if not ids:
        return "SCOPE_UNVERIFIED"
    snapshot = company_snapshot(db, ids, zones, now=now, operators=operator_labels(settings, ids),
                                excluded_store_count=excluded)
    value = fitted_company_card(snapshot)
    if value is None:
        return "CARD_TOO_LARGE"
    instant = now.astimezone(timezone.utc).replace(tzinfo=None)
    payload = dict(transport="app", fixed_route=True, delivery=target, open_ids=[], store_ids=ids,
                   card=value, scheduled_delivery_date=local.date().isoformat(), scheduled_timezone=BEIJING.key)
    if not delivery_current(db, settings, key, payload, instant, now, target["receive_id"]):
        return "SCOPE_UNVERIFIED"
    try:
        with db.begin_nested():
            db.add(NotificationOutbox(idempotency_key=key, chat_id=target["receive_id"],
                                       payload=payload, next_attempt_at=instant, created_at=instant))
            db.flush()
    except IntegrityError:
        # MariaDB REPEATABLE READ requires a current read after a collision.
        if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)
                     .with_for_update()) is None:
            raise
        return "ALREADY_QUEUED"
    return "QUEUED"


def digest_status(settings):
    """Owner-console projection: no secrets, no business numbers."""
    try:
        binding = _binding(settings)
    except (OSError, ValueError, TypeError, RecursionError):
        return {"enabled": bool(getattr(settings, "feishu_company_executive_digest_enabled", False)),
                "start_date": getattr(settings, "feishu_company_executive_digest_start_date", "") or None,
                "recipient": None, "configuration_valid": False, "store_count": None,
                "excluded_store_count": None}
    try:
        ids, _, excluded = _store_basis(settings, binding)
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        ids, excluded = None, None
    recipient = None
    if binding is not None:
        name = binding.get("name")
        recipient = {"id": binding["open_id"],
                     "name": name if isinstance(name, str) and name.strip() else binding["open_id"],
                     "verified_at": binding["verified_at"]}
    return {"enabled": bool(getattr(settings, "feishu_company_executive_digest_enabled", False)),
            "start_date": getattr(settings, "feishu_company_executive_digest_start_date", "") or None,
            "recipient": recipient, "configuration_valid": True,
            "store_count": len(ids) if ids is not None else None,
            "excluded_store_count": excluded, "schedule": "每天北京时间 08:30（10 分钟内送达窗口）",
            "marketplace_timezone": MARKETPLACE.key}
