"""Read-only account-health briefs, scoped to the configured stores."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.notifications.cards import card, number
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.routing import _load_routing, report_group_delivery
from jx_alert.storage.models import Store


HEALTH_HEADERS = ["店铺", "当前判定", "源评级（观测）", "健康评分", "绩效指标（观测）",
                  "关注项", "待处理通知（归档观测）", "源更新时间", "数据状态"]


def _percent(value):
    return "—" if value is None else number(value) + "%"


def health_overview_row(snapshot):
    readings = "；".join(item["label"] + " " + _percent(item["value"])
                         for item in snapshot["indicators"])
    quality = snapshot["freshness_label"]
    if snapshot["status"] == "UNCONFIRMED" and snapshot["confirmation_reason"] != quality:
        quality += "；" + snapshot["confirmation_reason"]
    return (snapshot["store_name"][:60] + "（" + snapshot["store_id"] + "）",
        snapshot["status_label"], snapshot["source_status_label"], number(snapshot["ahr_score"]),
        readings, snapshot["indicator_summary"], number(snapshot["pending_notice_observations"]),
        snapshot["source_updated_at"] or "—", quality)


def health_detail_rows(snapshot):
    rows = [
        ("健康评分", number(snapshot["ahr_score"]), snapshot["freshness_label"]),
        ("当前健康判定", snapshot["status_label"], snapshot["confirmation_reason"]),
        ("账户源状态", snapshot["source_status_label"], "源观测"),
        ("源更新时间", snapshot["source_updated_at"] or "—", snapshot["freshness_label"]),
        ("采集时间（UTC）", snapshot["captured_at"] or "—", "采集时间不代替源更新时间"),
        ("待处理通知（归档观测）", number(snapshot["pending_notice_observations"]),
         snapshot["notice_freshness_label"]),
    ]
    rows.extend((item["label"], _percent(item["value"]),
                 "平台标准 " + _percent(item["standard"]) + "；" + item["status_label"])
                for item in snapshot["indicators"])
    return rows


def health_brief_snapshots(db, settings, *, store_id=None, now=None):
    # Fail closed if the explicit scope cannot be loaded; never fall back to all
    # unrelated accounts just because a configuration file is absent.
    try:
        raw = json.loads(Path(settings.collection_scope_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Health brief requires a readable configured store scope") from exc
    ids = raw.get("store_ids") if isinstance(raw, dict) else None
    if (not isinstance(ids, list) or not ids
            or any(type(sid) not in (str, int) or not str(sid).isdigit() or int(sid) <= 0 for sid in ids)):
        raise ValueError("Health brief requires positive configured store IDs")
    ids = sorted({str(int(sid)) for sid in ids}, key=int)
    if store_id is not None:
        if str(store_id) not in ids:
            raise ValueError("Store is outside the configured health brief scope")
        ids = [str(store_id)]
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Health brief requires an aware clock")
    stores = {r.sid: r for r in db.scalars(select(Store).where(Store.sid.in_(ids)))}
    snapshots = []
    for sid in ids:
        store = stores.get(sid) or Store(sid=sid, name="未同步店铺", status=0)
        snapshot = store_health_snapshot(db, store, settings, now=clock)
        if sid not in stores:
            snapshot.update(status="NO_DATA", status_label="店铺未同步")
        snapshots.append(snapshot)
    counts = {}
    for item in snapshots:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"checked_at_utc": clock.isoformat(), "store_count": len(snapshots),
            "status_counts": counts, "observation_only": getattr(settings, "lingxing_performance_observation_only", True),
            "stores": snapshots}


def card_fits(value):
    """Include JSON-in-JSON escaping in the application's 20 KB wire limit."""
    payload = {"receive_id": "oc_" + "x" * 64, "msg_type": "interactive",
               "content": json.dumps(value, ensure_ascii=False), "uuid": "x" * 40}
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 20 * 1024


def build_health_brief_cards(snapshots, *, report_day):
    pages, offset = [], 0
    while offset < len(snapshots):
        size = min(10, len(snapshots) - offset)
        while size:
            value = card(f"亚马逊店铺绩效快报 · {report_day}", subtitle=f"店铺明细 · 第 {len(pages) + 1} 页",
                sections=[("店铺健康", HEALTH_HEADERS,
                           [health_overview_row(s) for s in snapshots[offset:offset + size]])])
            if card_fits(value):
                break
            size -= 1
        if not size:
            raise ValueError("A health brief row exceeds the message limit")
        pages.append((value, [s["store_id"] for s in snapshots[offset:offset + size]]))
        offset += size
    return pages


def enqueue_store_health_brief(db, settings, *, now=None):
    """Queue an explicitly requested brief once per Beijing day and destination."""
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Health brief requires an aware clock")
    day = clock.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    route = report_group_delivery(settings, _load_routing(settings.feishu_routing_file), role="store_health_brief")
    prefix = "store-health-brief:" + hashlib.sha256(f"{day}:{route['receive_id']}".encode()).hexdigest() + ":"
    existing = db.scalars(select(NotificationOutbox).where(
        NotificationOutbox.idempotency_key.startswith(prefix)).order_by(NotificationOutbox.id)).all()
    if existing:
        return {"status": "REUSED", "outbox_ids": [r.id for r in existing],
                "store_count": sum(len(r.payload["store_ids"]) for r in existing)}
    snapshots = health_brief_snapshots(db, settings, now=clock)
    pages = build_health_brief_cards(snapshots["stores"], report_day=day)
    ids = []
    # All pages commit together; a worker can never observe a partial brief.
    try:
        with db.begin_nested():
            for index, (value, store_ids) in enumerate(pages):
                row = NotificationOutbox(idempotency_key=prefix + str(index), chat_id=route["receive_id"],
                    payload={"text": f"亚马逊店铺绩效快报 · {day} · 第 {index + 1}/{len(pages)} 页",
                        "card": value, "store_ids": store_ids, "open_ids": [], "fixed_route": True,
                        "transport": "app", "delivery": route})
                db.add(row)
                db.flush()
                ids.append(row.id)
    except IntegrityError:
        existing = db.scalars(select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key.startswith(prefix)).order_by(NotificationOutbox.id)).all()
        if not existing:
            raise
        return {"status": "REUSED", "outbox_ids": [r.id for r in existing],
                "store_count": sum(len(r.payload["store_ids"]) for r in existing)}
    return {"status": "QUEUED", "outbox_ids": ids, "store_count": snapshots["store_count"],
            "status_counts": snapshots["status_counts"]}
