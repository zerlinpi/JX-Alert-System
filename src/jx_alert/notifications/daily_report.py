"""Queue read-only daily snapshots to an explicitly selected test/report group."""
import hashlib
import re
from datetime import date

from sqlalchemy import select

from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.notifications.routing import _load_routing
from jx_alert.storage.models import DailyMetric, DataCoverage, Store, StorePerformance


def enqueue_daily_snapshots(db, settings, *, report_date: date | str | None = None,
                            store_dates: dict[str, date | str] | None = None) -> dict:
    """Persist one snapshot per store/date/destination; never send from this function.

    Missing records stay unknown. The caller supplies marketplace-local report
    dates when they differ; this module never assumes missing metrics equal zero.
    """
    if not getattr(settings, "feishu_daily_report_enabled", False):
        return {"queued": 0, "skipped": True}
    chat_alias = getattr(settings, "feishu_daily_report_chat_id", "")
    if not chat_alias:
        raise ValueError("A daily snapshot group must be explicitly configured")
    routing = _load_routing(settings.feishu_routing_file)
    groups = routing.get("groups", {})
    app_transport = getattr(settings, "feishu_transport", "webhook") == "app"
    if app_transport:
        group = groups.get(chat_alias, {})
        chat_id = group.get("chat_id", chat_alias)
        if not isinstance(chat_id, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat_id):
            raise ValueError("Daily app snapshots require a configured group chat ID")
        delivery = {
            "role": "daily_snapshot", "receive_id": chat_id,
            "receive_id_type": "chat_id", "open_ids": [],
        }
    else:
        if chat_alias not in groups and chat_alias != "default":
            raise ValueError("Daily webhook snapshots require a configured group alias")
        chat_id, delivery = chat_alias, None

    target = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    if target is None and not store_dates:
        raise ValueError("Daily snapshots require a report date or per-store report dates")
    queued = 0
    stores_query = select(Store).order_by(Store.sid)
    if store_dates is not None:
        stores_query = stores_query.where(Store.sid.in_(store_dates))
    for store in db.scalars(stores_query).all():
        day = (store_dates or {}).get(store.sid, target)
        day = date.fromisoformat(day) if isinstance(day, str) else day
        rows = db.scalars(select(DailyMetric).where(
            DailyMetric.store_id == store.sid, DailyMetric.metric_date == day,
            DailyMetric.dimension.in_(["store", "campaign_sp"]),
        )).all()
        sales = {row.metric_name: row.value for row in rows
                 if row.dimension == "store" and row.entity_id == store.sid}
        ad_rows = [row for row in rows if row.dimension == "campaign_sp"]
        coverage = {
            row.dimension: row.status for row in db.scalars(select(DataCoverage).where(
                DataCoverage.store_id == store.sid, DataCoverage.metric_date == day,
                DataCoverage.dimension.in_(["store", "campaign_sp"]),
            )).all()
        }
        performance = db.scalar(select(StorePerformance).where(
            StorePerformance.store_id == store.sid,
        ).order_by(StorePerformance.pull_date.desc()).limit(1))

        def display(value):
            return "未知（无记录）" if value is None else f"{value:g}"

        spend_rows = [row.value for row in ad_rows if row.metric_name == "spend"]
        ad_spend = sum(spend_rows) if spend_rows else None
        lines = [
            f"【JX Alert】日常数据快照 · {day.isoformat()}",
            f"店铺：{store.name}（{store.sid}） · 币种：{store.currency_code or '未知'}",
            f"订单 {display(sales.get('orders'))} · 销量 {display(sales.get('units'))}"
            f" · 销售额 {display(sales.get('sales'))}",
            f"SP 已返回活动花费合计：{display(ad_spend)}",
            f"采集完整性：店铺 {coverage.get('store', '未知')}"
            f" / SP活动 {coverage.get('campaign_sp', '未知')}",
            "无记录或采集不完整均不代表真实零值；SP转化告警另按成熟归因日评估。",
        ]
        if performance:
            lines.append(
                f"绩效快照：{performance.pull_date} · "
                f"AHR {display(performance.ahr_score)} · {performance.freshness_status}"
            )
        else:
            lines.append("绩效快照：未知（尚无记录）")
        key = "daily:" + hashlib.sha256(
            f"{store.sid}:{day}:{'app' if app_transport else 'webhook'}:{chat_id}".encode()
        ).hexdigest()
        enqueue_notification(
            db, idempotency_key=key, text="\n".join(lines), store_id=store.sid,
            chat_id=chat_id, delivery=delivery, fixed_route=True,
        )
        queued += 1
    return {"queued": queued, "skipped": False}
