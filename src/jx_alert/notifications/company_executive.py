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
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.notifications.store_policy import paused_delivery
from jx_alert.reporting.company_executive import (
    BEIJING, CLOSES, LATEST, MARKETPLACE, METRICS, OPENS, company_snapshot,
)
from jx_alert.storage.models import Store

PREFIX = "scheduled-daily:company-executive:"
ROLE = "company_executive_digest"
BLOCKED = "公司日报私聊权限或店铺范围已变更，保留旧记录，不迁移或补发"
SECTION = "company_executive_digest"
_LABELS = {"sales": "销售额", "orders": "订单量¹", "spend": "广告花费（SP/SB/SD）",
           "acos": "ACOS", "tacos": "TACOS", "cvr": "CVR（仅 SP 点击转化率）",
           "ppc": "PPC（CPC，单次点击花费）"}


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
    if (not isinstance(values, list) or not 1 <= len(values) <= 100
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


def _store_basis(settings):
    ids = _scope(_json_file(settings.collection_scope_file).get("store_ids"))
    zones = _json_file(settings.store_timezones_file).get("stores")
    if not isinstance(zones, dict):
        raise ValueError("Company digest timezone configuration unavailable")
    return ids, {sid: (zones.get(sid) or {}).get("timezone") for sid in ids
                 if isinstance(zones.get(sid), dict)}


def preview_company_digest(db, settings, *, now):
    """Pure preview using local configured scope; neither recipient config nor outbox needed."""
    ids, zones = _store_basis(settings)
    result = company_snapshot(db, ids, zones, now=now)
    return result, company_card(result)


def _route(settings, now, ids):
    if (not getattr(settings, "feishu_company_executive_digest_enabled", False)
            or not settings.scheduler_enabled or settings.app_dry_run or not settings.feishu_enabled
            or settings.feishu_transport != "app"):
        return None
    try:
        raw_start = settings.feishu_company_executive_digest_start_date
        start = date.fromisoformat(raw_start)
        if raw_start != start.isoformat() or now.astimezone(BEIJING).date() < start:
            return None
        binding = _json_file(settings.feishu_routing_file).get(SECTION)
        if not isinstance(binding, dict) or set(binding) != {"open_id", "app_id", "generation", "verified_at", "store_ids"}:
            return None
        opened = binding["open_id"]
        app_id = binding["app_id"]
        generation = binding["generation"]
        stamp = datetime.fromisoformat(binding["verified_at"])
        if (not isinstance(opened, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,150}", opened)
                or not isinstance(app_id, str) or not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,150}", app_id)
                or not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation)
                or stamp.tzinfo is None or stamp.utcoffset() is None or stamp > now
                or _scope(binding["store_ids"]) != ids):
            return None
        identity = hashlib.sha256(app_id.encode("utf-8")).hexdigest()
        if FeishuAppClient.configured_app_identity(settings) != identity:
            return None
        return dict(role=ROLE, receive_id_type="open_id", receive_id=opened, open_ids=[],
                    generation=generation, verified_at=stamp.astimezone(timezone.utc).isoformat(),
                    app_identity_sha256=identity)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, OverflowError, RecursionError):
        return None


def _store_authorized(db, ids, zones):
    if any(zones.get(sid) != MARKETPLACE.key for sid in ids):
        return False
    authorized = set(db.scalars(select(Store.sid).where(
        Store.sid.in_(ids), Store.status == 1, Store.timezone == MARKETPLACE.key,
    )))
    return authorized == set(ids)


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
        ids, zones = _store_basis(settings)
        current = _route(settings, instant, ids)
        if (current is None or current != payload.get("delivery") or current["receive_id"] != chat_id
                or ids != payload.get("store_ids") or not _store_authorized(db, ids, zones)):
            return False
        created = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
        return (datetime.fromisoformat(current["verified_at"]) <= created <= instant
                and not paused_delivery(settings, payload, created_at))
    except (OSError, TypeError, ValueError, RuntimeError, RecursionError):
        return False


def company_card(snapshot):
    def metric_text(name, value, code):
        if value is None:
            return "—"
        if name == "orders":
            return f"{value:,}"
        if name in {"acos", "tacos", "cvr"}:
            return f"{value:.2%}"
        return f"{value:,.2f} {code}"

    order_label = "订单量¹"
    across = snapshot["company_orders"]
    subtitle = (f"北京时间 08:30 · 公司已配置店铺 {snapshot['store_count']} 家\n"
                f"最近已完结经营日：{snapshot['date']}（美西）\n"
                f"全范围{order_label}：" + (f"{across:,}" if across is not None else
                    f"不汇总（{snapshot['orders_covered']}/{snapshot['store_count']} 家有完整依据）"))
    latest, settled = [], []
    for code, data in snapshot["currencies"].items():
        for metric in METRICS:
            current = metric in LATEST
            rows = latest if current else settled
            rows.append((code, _LABELS[metric], metric_text(metric, data["values"][metric], code),
                         f"{data['covered'][metric]}/{data['store_count']} 店"))
    notes = [
        "¹ 订单量为按 ASIN 商品汇总，非唯一订单去重；不同币种不换算、不合计。",
        "TACOS=同日 SP/SB/SD 广告花费÷店铺销售额；PPC 按 CPC 算式显示，为混合花费÷点击，可能含按展示付费。",
        f"最近经营日的 ACOS 和 CVR 不显示：归因尚未成熟。成熟页仅指 {snapshot['mature_date']}，"
        "ACOS=SP/SB/SD 花费÷同日归因销售额；CVR=SP 点击归因订单÷同日 SP 点击，均先汇总再算。",
        "覆盖为具备该指标计算所需事实的店铺数；— 表示覆盖不足、无明确事实或分母为零，不表示零。",
    ]
    if snapshot["unconfirmed_currency_stores"]:
        notes.insert(0, f"{snapshot['unconfirmed_currency_stores']} 家店铺币种或销售依据待确认，不展示不完整的币种合计。")
    return card("公司经营与广告快报", subtitle=subtitle,
        sections=[
            (f"最近已完结经营日 · {snapshot['date']}（美西）", ["币种", "指标", "数值", "覆盖"], latest),
            (f"成熟归因日 · {snapshot['mature_date']}（美西）", ["币种", "指标", "数值", "覆盖"], settled),
        ], notes=notes)


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
    ids, zones = _store_basis(settings)
    target = _route(settings, now, ids)
    if target is None:
        return "ROUTE_UNVERIFIED"
    snapshot = company_snapshot(db, ids, zones, now=now)
    value = company_card(snapshot)
    if not card_fits(value):
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
