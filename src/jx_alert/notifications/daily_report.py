"""Queue read-only daily snapshots to an explicitly selected test/report group."""
import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.notifications.outbox import enqueue_notification, NotificationOutbox
from jx_alert.notifications.routing import _load_routing
from jx_alert.storage.models import DailyMetric, DataCoverage, Store, StorePerformance
from jx_alert.notifications.cards import AD_KPI_HEADERS, ad_kpi_rows, card, number, change, status_label
from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.notifications.store_health import HEALTH_HEADERS, card_fits, health_detail_rows, health_overview_row


def store_advertising_kpis(db, store, day, *, settings=None, data_warning=None):
    if settings is None:
        from jx_alert.config import get_settings
        settings = get_settings()
    result = advertising_kpis(db, settings, store.sid, day)
    if data_warning:
        for item in result.values():
            item.update(current=None, baseline=None, change_ratio=None, status="INSUFFICIENT_DATA")
    return result


def _metric_currency(facts, on, dimension, metric):
    """Currency belongs to the observed metric, not the store's country/sales setting."""
    values = [r.currency for r in facts if r.metric_date == on and r.dimension == dimension
              and r.metric_name == metric]
    if (not values or any(not isinstance(v, str) or not re.fullmatch(r"[A-Z]{3}", v) for v in values)
            or len(set(values)) != 1):
        return None
    return values[0]


def build_daily_card(db, store, day, *, data_warning=None, settings=None):
    """Read two authoritative daily partitions; never fill missing facts with zero."""
    if settings is None:
        from jx_alert.config import get_settings
        settings = get_settings()
    previous = day - timedelta(days=1)
    dimensions = ["store", "campaign_sp", "campaign_sb", "campaign_sd"]
    facts = db.scalars(select(DailyMetric).where(
        DailyMetric.store_id == store.sid, DailyMetric.metric_date.in_([day, previous]),
        DailyMetric.dimension.in_(dimensions),
    )).all()
    coverage_rows = {(r.metric_date, r.dimension): r for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store.sid, DataCoverage.metric_date.in_([day, previous]),
        DataCoverage.dimension.in_(dimensions),
    ))}
    coverage = {key: row.status for key, row in coverage_rows.items()}

    def empty_partition(on, dimension):
        record = coverage_rows.get((on, dimension))
        return (not data_warning and record is not None and record.status == "COMPLETE" and record.row_count == 0
                and not any(r.metric_date == on and r.dimension == dimension for r in facts))

    def total(on, dimension, metric):
        if data_warning or coverage.get((on, dimension)) != "COMPLETE":
            return None
        values = [r.value for r in facts if r.metric_date == on and r.dimension == dimension
                  and r.metric_name == metric and (dimension != "store" or r.entity_id == store.sid)]
        return sum(values) if values else None

    metrics = []
    for label, dimension, metric, money in [
        ("订单", "store", "orders", False), ("销量", "store", "units", False),
        ("销售额", "store", "sales", True), ("商品推广花费", "campaign_sp", "spend", True),
        ("品牌推广花费", "campaign_sb", "spend", True), ("展示推广花费", "campaign_sd", "spend", True),
    ]:
        current, prior = total(day, dimension, metric), total(previous, dimension, metric)
        complete = all(coverage.get((d, dimension)) == "COMPLETE" for d in [day, previous])
        current_currency = _metric_currency(facts, day, dimension, metric) if money else None
        prior_currency = _metric_currency(facts, previous, dimension, metric) if money else None
        def display(value, currency, on):
            if empty_partition(on, dimension):
                return "接口无记录"
            if value is not None and money and currency is None:
                return "未知（币种待确认）"
            return number(value, money=money, currency=currency or "")
        comparison = change(current, prior, complete=complete)
        if empty_partition(day, dimension) or empty_partition(previous, dimension):
            comparison = "接口无记录，无法比较"
        if money and current is not None and prior is not None and complete:
            if not current_currency or not prior_currency:
                comparison = "币种待确认"
            elif current_currency != prior_currency:
                comparison = "币种不一致"
        metrics.append((label, display(current, current_currency, day), display(prior, prior_currency, previous), comparison))
    quality = [(label, "待重新采集校验" if data_warning else "接口无记录" if empty_partition(day, dimension)
                else status_label(coverage.get((day, dimension), "未知"))) for label, dimension in [
        ("店铺销量", "store"), ("商品推广", "campaign_sp"), ("品牌推广", "campaign_sb"), ("展示推广", "campaign_sd")]]
    zone_label = ("美国太平洋时间" if store.timezone == "America/Los_Angeles" else
                  "店铺当地时间" if store.timezone else "保守历史日 · 时区待核实")
    notes = [f"{zone_label} · 对比日期：{previous.isoformat()}"]
    if data_warning:
        notes.insert(0, data_warning)
    sections = [("经营与广告", ["指标", "当日", "前一日", "环比"], metrics),
                ("采集状态", ["数据集", "状态"], quality)]
    health = store_health_snapshot(db, store, settings)
    sections.append(("店铺绩效快报", ["指标", "观测值", "判定或依据"], health_detail_rows(health)))
    kpis = store_advertising_kpis(db, store, day, settings=settings, data_warning=data_warning)
    sections.append(("广告指标 · 转化指标采用成熟日", AD_KPI_HEADERS, ad_kpi_rows(kpis)))
    return card(
        f"每日经营快照 · {day.isoformat()}",
        subtitle=f"{store.name}（{store.sid}）",
        sections=sections, notes=notes,
    )


def _enqueue_group_digest(db, stores, dates, warnings, *, send_day, due, chat_id, delivery, settings):
    """Freeze all chunks in one transaction; recovery reuses the original scope/content."""
    prefix = "scheduled-daily:group:" + hashlib.sha256(f"{send_day}:app:{chat_id}".encode()).hexdigest() + ":"
    existing = db.scalars(select(NotificationOutbox).where(
        NotificationOutbox.idempotency_key.startswith(prefix)).order_by(NotificationOutbox.id)).all()
    if existing:
        return {"queued": len(existing), "skipped": False, "outbox_ids": [r.id for r in existing]}
    # Do not produce a second format after a legacy per-store report was queued today.
    legacy = db.scalars(select(NotificationOutbox).where(
        NotificationOutbox.idempotency_key.startswith("scheduled-daily:"),
        NotificationOutbox.chat_id == chat_id,
        NotificationOutbox.payload["scheduled_delivery_date"].as_string() == send_day.isoformat())).all()
    if legacy:
        return {"queued": len(legacy), "skipped": False, "outbox_ids": [r.id for r in legacy]}
    rows, kpi_rows, health_rows, ids = [], [], [], []
    for store in sorted(stores, key=lambda s: s.sid):
        day = dates[store.sid]
        day = date.fromisoformat(day) if isinstance(day, str) else day
        source = build_daily_card(db, store, day, data_warning=warnings.get(store.sid), settings=settings)
        tables = [e for e in source["body"]["elements"] if e["tag"] == "table"]
        values = [r["c1"] for r in tables[0]["rows"]]
        quality = "待校验" if warnings.get(store.sid) else (
            "采集完成" if all(r["c1"] in {"采集完成", "接口无记录"} for r in tables[1]["rows"]) else "数据不完整")
        if not store.timezone:
            quality += "；时区待核实"
        rows.append((f"{store.name[:60]}（{store.sid}）", day.isoformat(), *values, quality))
        ratios = tables[-1]["rows"]
        groups = {}
        for code, row in zip(("CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS"), ratios):
            if row["c4"] != "—":
                groups.setdefault(row["c4"], []).append(code)
        judgment = "；".join(label + "：" + "、".join(codes) for label, codes in groups.items()) or "—"
        kpi_rows.append((f"{store.name[:60]}（{store.sid}）",
            ratios[0]["c5"] + " / " + ratios[4]["c5"], *[row["c1"] for row in ratios], judgment))
        health_rows.append(health_overview_row(store_health_snapshot(db, store, settings)))
        ids.append(store.sid)
    # Bound both table size and encoded payload. No cross-currency totals are formed.
    chunks = []
    headers = ["店铺", "报表日期", "订单", "销量", "销售额", "商品推广", "品牌推广", "展示推广", "状态"]
    offset = 0
    while offset < len(rows):
        size = min(5, len(rows) - offset)
        while size:
            value = card(f"每日经营日报 · {send_day}", subtitle=f"店铺明细 · 第 {len(chunks) + 1} 页",
                     sections=[("经营与广告", headers, rows[offset:offset + size]),
                        ("店铺绩效快报", HEALTH_HEADERS, health_rows[offset:offset + size]),
                        ("广告指标", ["店铺", "流量及费用日 / 成熟归因日",
                         "CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS", "判断"], kpi_rows[offset:offset + size])])
            if card_fits(value):
                break
            size -= 1
        if not size:
            raise ValueError("Daily digest exceeds the bounded card size")
        chunks.append((value, ids[offset:offset + size]))
        offset += size
    inserted = []
    try:
        # A concurrent producer either wins the whole batch or reuses it below.
        with db.begin_nested():
            for index, (value, store_ids) in enumerate(chunks):
                row = NotificationOutbox(idempotency_key=prefix + str(index), chat_id=chat_id,
                    status="PENDING", next_attempt_at=due.astimezone(timezone.utc).replace(tzinfo=None),
                    payload={"text": f"每日经营日报 · {send_day} · 第 {index + 1}/{len(chunks)} 页",
                        "store_ids": store_ids, "open_ids": [], "fixed_route": True,
                        "transport": "app", "delivery": delivery, "card": value,
                        "scheduled_delivery_date": send_day.isoformat(), "scheduled_timezone": "Asia/Shanghai",
                        "scheduled_not_before": due.isoformat()})
                db.add(row)
                db.flush()
                inserted.append(row.id)
    except IntegrityError:
        existing = db.scalars(select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key.startswith(prefix)).order_by(NotificationOutbox.id)).all()
        if not existing:
            raise
        inserted = [r.id for r in existing]
    return {"queued": len(inserted), "skipped": False, "outbox_ids": inserted}


def enqueue_daily_snapshots(db, settings, *, report_date: date | str | None = None,
                            store_dates: dict[str, date | str] | None = None,
                            delivery_date: date | str | None = None,
                            now: datetime | None = None,
                            store_records: list[Store] | None = None,
                            store_warnings: dict[str, str] | None = None) -> dict:
    """Persist one snapshot per store/date/destination; never send from this function.

    Missing records stay unknown. The caller supplies marketplace-local report
    dates when they differ; this module never assumes missing metrics equal zero.
    """
    if not getattr(settings, "feishu_daily_report_enabled", False):
        return {"queued": 0, "skipped": True, "outbox_ids": []}
    send_day = date.fromisoformat(delivery_date) if isinstance(delivery_date, str) else delivery_date
    due = local_now = None
    if send_day is not None:
        local_now = now or datetime.now(timezone.utc)
        if local_now.tzinfo is None:
            raise ValueError("Scheduled delivery requires an aware clock")
        local_now = local_now.astimezone(ZoneInfo("Asia/Shanghai"))
        start = getattr(settings, "feishu_daily_report_start_date", "")
        if start and send_day < date.fromisoformat(start):
            return {"queued": 0, "skipped": True, "outbox_ids": [], "reason": "before_start_date"}
        if send_day < local_now.date():
            raise ValueError("Past delivery dates require explicit manual review")
        trigger = CronTrigger.from_crontab(
            getattr(settings, "feishu_daily_report_cron", "0 8 * * *"), timezone="Asia/Shanghai")
        due = trigger.get_next_fire_time(None, datetime.combine(send_day, time.min, ZoneInfo("Asia/Shanghai")))
        if due is None or due.date() != send_day:
            raise ValueError("Daily report cron has no delivery window on this date")
        if local_now < due:
            return {"queued": 0, "skipped": True, "outbox_ids": [], "reason": "before_scheduled_time"}
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
    queued, outbox_ids = 0, []
    stores_query = select(Store).order_by(Store.sid)
    if store_dates is not None:
        stores_query = stores_query.where(Store.sid.in_(store_dates))
    stores = store_records if store_records is not None else db.scalars(stores_query).all()
    if (send_day is not None and app_transport and len(stores) > 1
            and getattr(settings, "feishu_daily_report_group_digest", False)):
        dates = store_dates if store_dates is not None else {s.sid: target for s in stores}
        if any(s.sid not in dates for s in stores):
            raise ValueError("Daily report store is outside the explicit report scope")
        return _enqueue_group_digest(db, stores, dates, store_warnings or {},
            send_day=send_day, due=due, chat_id=chat_id, delivery=delivery, settings=settings)
    for store in stores:
        if store_dates is not None and store.sid not in store_dates:
            raise ValueError("Daily report store is outside the explicit report scope")
        warning = (store_warnings or {}).get(store.sid)
        day = (store_dates or {}).get(store.sid, target)
        day = date.fromisoformat(day) if isinstance(day, str) else day
        prefix = "scheduled-daily:" if send_day is not None else "daily:"
        identity_day = send_day if send_day is not None else day
        key = prefix + hashlib.sha256(
            f"{store.sid}:{identity_day}:{'app' if app_transport else 'webhook'}:{chat_id}".encode()
        ).hexdigest()
        existing = db.scalar(select(NotificationOutbox).where(NotificationOutbox.idempotency_key == key))
        if existing is not None:
            # Recovery checks reuse persisted content and delivery state, even if
            # newer metrics arrived. They never regenerate or rearm a report.
            outbox_ids.append(existing.id)
            queued += 1
            continue
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
        if warning or coverage.get("store") != "COMPLETE":
            sales = {}
        performance = db.scalar(select(StorePerformance).where(
            StorePerformance.store_id == store.sid,
        ).order_by(StorePerformance.pull_date.desc()).limit(1))

        def display(value):
            return "未知（无记录）" if value is None else f"{value:g}"

        spend_rows = [row.value for row in ad_rows if row.metric_name == "spend"] if (
            not warning and coverage.get("campaign_sp") == "COMPLETE") else []
        ad_spend = sum(spend_rows) if spend_rows else None
        ad_currency = _metric_currency(ad_rows, day, "campaign_sp", "spend")
        ad_display = ("未知（币种待确认）" if ad_spend is not None and not ad_currency
                      else number(ad_spend, money=True, currency=ad_currency or ""))
        lines = [
            f"日常经营日报 · {day.isoformat()}",
            f"店铺：{store.name}（{store.sid}）",
            f"订单 {display(sales.get('orders'))} · 销量 {display(sales.get('units'))}"
            f" · 销售额 {display(sales.get('sales'))}",
            f"商品推广花费：{ad_display}",
            f"采集状态：店铺 {status_label(coverage.get('store'))}"
            f" / 商品推广 {status_label(coverage.get('campaign_sp'))}",
        ]
        if performance:
            lines.append(
                f"绩效快照：{performance.pull_date} · "
                f"健康评分 {display(performance.ahr_score)} · {status_label(performance.freshness_status)}"
            )
        else:
            lines.append("绩效快照：未知（尚无记录）")
        if warning:
            lines.append(warning)
        if not store.timezone:
            lines.append("保守历史日 · 报表时区待核实")
        message_card = build_daily_card(db, store, day, data_warning=warning, settings=settings) if app_transport else None
        if send_day is not None:
            delivery_note = f"北京时间提醒日：{send_day} · 计划 {due:%H:%M} · 本次生成 {local_now:%H:%M:%S}"
            if local_now - due > timedelta(minutes=1):
                delivery_note += "（延迟补发）"
            lines.append(delivery_note)
            if message_card is not None:
                from jx_alert.notifications.cards import plain
                message_card["body"]["elements"].append({"tag": "markdown", "content": plain(delivery_note)})
        row = enqueue_notification(
            db, idempotency_key=key, text="\n".join(lines), store_id=store.sid,
            chat_id=chat_id, delivery=delivery, fixed_route=True,
            card=message_card,
        )
        if send_day is not None and existing is None:
            row.next_attempt_at = due.astimezone(timezone.utc).replace(tzinfo=None)
            row.payload = {**row.payload, "scheduled_delivery_date": send_day.isoformat(),
                           "scheduled_timezone": "Asia/Shanghai", "scheduled_not_before": due.isoformat()}
        db.flush()
        outbox_ids.append(row.id)
        queued += 1
    return {"queued": queued, "skipped": False, "outbox_ids": outbox_ids}
