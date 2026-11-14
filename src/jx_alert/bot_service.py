"""Durable group questions. No model request runs in MCP or event callbacks."""
from datetime import date, datetime, timedelta, timezone
import html
import json
import math
import re
import time

from sqlalchemy import func, select

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.chat_analysis import _scope
from jx_alert.notifications.cards import card, number
from jx_alert.notifications.daily_report import build_daily_card
from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.notifications.routing import _load_routing, report_group_delivery
from jx_alert.notifications.store_health import card_fits, health_brief_snapshots
from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, Store, SyncJob


def _utcnow():
    return datetime.now(timezone.utc)


def _route(settings):
    return report_group_delivery(settings, _load_routing(settings.feishu_routing_file), role="group_question")


def _reply(db, settings, message_id, chat_id, cards):
    delivery = _route(settings)
    if delivery["receive_id"] != chat_id:
        raise ValueError("Original question is outside the current allowed group")
    delivery = {**delivery, "reply_to_message_id": message_id}
    ids = []
    for index, value in enumerate(cards):
        row = enqueue_notification(db, idempotency_key=f"group-question:{message_id}:{index}",
            text=value["header"]["title"]["content"], chat_id=chat_id, card=value,
            delivery=delivery, fixed_route=True)
        ids.append(row.id)
    return ids


def enqueue_group_question(db, settings, event_dict):
    """Validate the group and real bot identity again; persist only needed fields."""
    if not getattr(settings, "feishu_bot_enabled", False) or settings.app_dry_run or not settings.feishu_enabled:
        return {"status": "DISABLED"}
    if not isinstance(event_dict, dict):
        return {"status": "IGNORED"}
    header, event = event_dict.get("header", {}), event_dict.get("event", {})
    if not isinstance(header, dict) or not isinstance(event, dict):
        return {"status": "IGNORED"}
    sender, message = event.get("sender", {}), event.get("message", {})
    if (header.get("event_type") != "im.message.receive_v1" or header.get("app_id") != settings.feishu_app_id
            or not isinstance(sender, dict) or not isinstance(message, dict)
            or sender.get("sender_type") != "user" or message.get("chat_type") != "group"
            or message.get("message_type") != "text" or message.get("chat_id") != _route(settings)["receive_id"]):
        return {"status": "IGNORED"}
    mentions = message.get("mentions", [])
    own_id = getattr(settings, "feishu_bot_open_id", "")
    if not own_id or not isinstance(mentions, list) or not any(
            isinstance(m, dict) and isinstance(m.get("id"), dict) and m["id"].get("open_id") == own_id
            for m in mentions):
        return {"status": "IGNORED"}
    message_id = message.get("message_id")
    sender_id = sender.get("sender_id", {}).get("open_id") if isinstance(sender.get("sender_id"), dict) else None
    if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
        return {"status": "IGNORED"}
    if not isinstance(sender_id, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", sender_id):
        return {"status": "IGNORED"}
    try:
        content = json.loads(message.get("content", ""))
        if not isinstance(content, dict):
            return {"status": "IGNORED"}
        question = content.get("text", "")
    except (TypeError, ValueError):
        return {"status": "IGNORED"}
    if not isinstance(question, str):
        return {"status": "IGNORED"}
    for mention in mentions:
        if isinstance(mention, dict) and isinstance(mention.get("key"), str):
            question = question.replace(mention["key"], "")
    question = question.strip()
    if not question or len(question) > 1200:
        return {"status": "IGNORED"}
    clock = _utcnow().replace(tzinfo=None)
    with database_job_lock(db.get_bind(), "feishu_question_ingest"):
        existing = db.scalar(select(SyncJob).where(SyncJob.job_type == "feishu_question",
            SyncJob.metadata_json["message_id"].as_string() == message_id))
        if existing:
            return {"status": "REUSED", "job_id": existing.id}
        pending = db.scalar(select(func.count(SyncJob.id)).where(SyncJob.job_type == "feishu_question",
            SyncJob.status.in_(["PENDING", "RUNNING"])))
        recent = db.scalar(select(func.count(SyncJob.id)).where(SyncJob.job_type == "feishu_question",
            SyncJob.started_at >= clock - timedelta(minutes=1),
            SyncJob.metadata_json["sender_id"].as_string() == sender_id))
        busy = pending >= getattr(settings, "feishu_bot_max_pending", 50) or recent >= 3
        job = SyncJob(job_type="feishu_question", status="SUCCEEDED" if busy else "PENDING",
            started_at=clock, metadata_json={"message_id": message_id, "sender_id": sender_id,
                "chat_id": message["chat_id"], "question": question, "attempts": 0})
        db.add(job)
        db.flush()
        if busy:
            value = card("提问较多", subtitle="请稍后重试", sections=[])
            ids = _reply(db, settings, message_id, message["chat_id"], [value])
            job.metadata_json = {**job.metadata_json, "outbox_ids": ids}
            job.finished_at = clock
        db.commit()  # Publish deduplication while the ingestion lock is held.
        return {"status": "BUSY" if busy else "QUEUED", "job_id": job.id}


def _resolve_store(db, settings, question):
    snapshots = health_brief_snapshots(db, settings)["stores"]
    countries = {s.sid: s.country for s in db.scalars(select(Store).where(Store.sid.in_([s["store_id"] for s in snapshots])))}
    for snapshot in snapshots:
        snapshot["country"] = countries.get(snapshot["store_id"]) or "待确认"
    ids = [s["store_id"] for s in snapshots if re.search(r"(?<!\d)" + re.escape(s["store_id"]) + r"(?!\d)", question)]
    if not ids:
        ids = [s["store_id"] for s in snapshots if s["store_name"] and s["store_name"].casefold() in question.casefold()]
        if len(ids) > 1:
            localized = [s["store_id"] for s in snapshots if s["store_id"] in ids and s["country"] != "待确认"
                and re.search((r"(?<![A-Za-z])" + re.escape(s["country"]) + r"(?![A-Za-z])")
                    if s["country"].isascii() else re.escape(s["country"]), question, re.IGNORECASE)]
            if localized:
                ids = localized
    return snapshots, list(dict.fromkeys(ids))


def _ad_totals(db, store_id, on):
    metrics = ("spend", "orders", "units", "sales", "clicks", "impressions")
    observations = db.scalars(select(DailyMetric).where(DailyMetric.store_id == store_id,
        DailyMetric.metric_date == on, DailyMetric.dimension.in_(["campaign_sp", "campaign_sb", "campaign_sd"]),
        DailyMetric.metric_name.in_(metrics))).all()
    coverage = {c.dimension: c for c in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store_id, DataCoverage.metric_date == on))}
    totals = []
    for kind, label in (("sp", "商品推广"), ("sb", "品牌推广"), ("sd", "展示推广")):
        dimension = "campaign_" + kind
        rows = [r for r in observations if r.dimension == dimension]
        entities = {r.entity_id for r in rows}
        record = coverage.get(dimension)
        complete = record is not None and record.status == "COMPLETE" and record.row_count <= len(entities)
        item = {"ad_type": label, "report_date": on.isoformat(), "coverage": record.status if record else "MISSING"}
        for metric in metrics:
            selected = [r for r in rows if r.metric_name == metric]
            valid = (complete and selected and {r.entity_id for r in selected} == entities
                     and all(math.isfinite(r.value) and r.value >= 0 for r in selected))
            item[metric] = math.fsum(r.value for r in selected) if valid else None
            if metric in {"sales", "spend"}:
                codes = {r.currency for r in selected}
                item[metric + "_currency"] = next(iter(codes)) if len(codes) == 1 else None
                if not item[metric + "_currency"] or not re.fullmatch(r"[A-Z]{3}", item[metric + "_currency"]):
                    item[metric] = None
        totals.append(item)
    return totals


def question_context(db, settings, store_id, *, now=None):
    clock = now or _utcnow()
    zone = _scope(settings, store_id, now=clock)
    store = db.get(Store, store_id)
    if store is None:
        raise ValueError("Store has not been synchronized")
    target = date.fromisoformat(zone["report_date"])
    warning = None if store.status == 1 else "店铺授权或启用状态异常，最新经营数据待确认"
    value = build_daily_card(db, store, target, settings=settings, data_warning=warning)
    tables = [e for e in value["body"]["elements"] if e["tag"] == "table"]
    kpis = advertising_kpis(db, settings, store_id, target)
    totals = _ad_totals(db, store_id, target)
    if warning:
        for kpi in kpis.values():
            kpi.update(current=None, baseline=None, status="INSUFFICIENT_DATA", change_ratio=None)
        for row in totals:
            row.update({m: None for m in ("spend", "orders", "units", "sales", "clicks", "impressions")})
    candidates = db.scalars(select(Campaign).where(Campaign.store_id == store_id)).all()
    masters = {(c.campaign_type, c.campaign_id): c for c in candidates}
    highest_spend = db.scalars(select(DailyMetric).where(DailyMetric.store_id == store_id,
        DailyMetric.metric_date == target, DailyMetric.dimension.in_(["campaign_sp", "campaign_sb", "campaign_sd"]),
        DailyMetric.metric_name == "spend").order_by(DailyMetric.value.desc()).limit(10)).all() if not warning else []
    campaigns = []
    for fact in highest_spend:
        kind = fact.dimension.removeprefix("campaign_")
        master = masters.get((kind, fact.entity_id))
        campaigns.append({"campaign_id": fact.entity_id, "name": master.name if master else fact.entity_id,
            "ad_type": kind, "current_state": master.state if master else None,
            "kpis": advertising_kpis(db, settings, store_id, target, dimension=fact.dimension, entity_id=fact.entity_id)})
    screening = []
    for entity, name, comparisons in [(store_id, store.name, kpis),
            *[(c["campaign_id"], c["name"], c["kpis"]) for c in campaigns]]:
        for metric, item in comparisons.items():
            if item["status"] == "ADVERSE":
                screening.append({"entity_id": entity, "name": name, "metric": metric,
                    **{key: item[key] for key in ("current", "baseline", "current_date", "currency", "change_ratio", "title")}})
    return {"store_id": store_id, "store_name": store.name, "country": store.country,
        "report_date": target.isoformat(), "date_basis": zone["date_basis"],
        "timezone_confirmed": zone["timezone_confirmed"], "authorization_warning": warning,
        "snapshot_at_utc": clock.isoformat(), "sales_and_spend": tables[0]["rows"],
        "coverage": tables[1]["rows"], "advertising_totals": totals, "advertising_kpis": kpis,
        "health": store_health_snapshot(db, store, settings, now=clock),
        "screening_candidates": screening, "screening_candidate_count": len(screening),
        "campaigns": campaigns, "campaign_count": len(candidates),
        "campaign_selection": "仅当前花费最高的至多10个活动样本，不代表全活动逐一巡检"}, value


def _answer_cards(context, data_card, analysis):
    # These figures are rendered by the service, not regenerated by the model.
    totals = [(r["ad_type"], number(r["spend"], money=True, currency=r["spend_currency"] or ""),
               number(r["orders"]), number(r["units"]), number(r["sales"], money=True, currency=r["sales_currency"] or ""),
               number(r["clicks"]), number(r["impressions"])) for r in context["advertising_totals"]]
    raw = card("广告归因数据", subtitle=context["store_name"] + " · " + context["report_date"],
        sections=[("广告明细", ["广告类型", "花费", "归因订单", "归因销量", "归因销售额", "点击", "曝光"], totals)])
    # Combine all required data when it fits. Preserve separate dates in KPI rows.
    combined = {**data_card, "body": {"elements": data_card["body"]["elements"] + raw["body"]["elements"][1:]}}
    cards = [combined] if card_fits(combined) else [data_card, raw]
    if analysis:
        text = html.escape(analysis, quote=False)
        # Small slices bound UTF-8 and JSON escaping even for Chinese responses.
        pieces = []
        while text:
            end = len(text) if len(text) <= 2400 else text.rfind("\n", 0, 2400)
            end = end if end > 0 else min(2400, len(text))
            pieces.append(text[:end])
            text = text[end:].lstrip("\n")
        for piece in pieces:
            value = card("店铺分析", subtitle=context["store_name"], sections=[])
            value["body"]["elements"].append({"tag": "markdown", "content": piece})
            cards.append(value)
    if not all(card_fits(value) for value in cards):
        raise ValueError("Group answer exceeds the bounded message size")
    return cards


def process_one_question(settings, session_factory, warehouse_engine, *, client_factory=None):
    if not settings.feishu_bot_enabled or settings.app_dry_run or not settings.feishu_enabled:
        return {"status": "DISABLED"}
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError
    factory = client_factory or DeepSeekClient.from_settings
    with database_job_lock(warehouse_engine, "feishu_question_worker"):
        with session_factory() as db:
            abandoned = db.scalars(select(SyncJob).where(SyncJob.job_type == "feishu_question", SyncJob.status == "RUNNING")).all()
            for row in abandoned:
                attempts = (row.metadata_json or {}).get("attempts", 0)
                row.status = "FAILED" if attempts >= 2 else "PENDING"
                if row.status == "FAILED":
                    row.finished_at = _utcnow().replace(tzinfo=None)
            db.commit()
            job = db.scalar(select(SyncJob).where(SyncJob.job_type == "feishu_question", SyncJob.status == "PENDING")
                            .order_by(SyncJob.id).limit(1))
            if job is None:
                return {"status": "IDLE"}
            if _utcnow().replace(tzinfo=None) - job.started_at > timedelta(minutes=settings.feishu_bot_question_max_age_minutes):
                job.status, job.finished_at = "EXPIRED", _utcnow().replace(tzinfo=None)
                db.commit()
                return {"status": "EXPIRED"}
            job.status = "RUNNING"
            meta, job_id = dict(job.metadata_json), job.id
            meta["attempts"] = meta.get("attempts", 0) + 1
            job.metadata_json = meta
            db.commit()
            snapshots, ids = _resolve_store(db, settings, meta["question"])
            if len(ids) != 1:
                options = [s for s in snapshots if s["store_id"] in ids]
                rows = [(s["store_id"], s["store_name"], s["country"]) for s in options[:20]]
                value = card("请确认店铺", subtitle="请在提问中注明店铺ID，例如：5174 店铺广告和绩效情况",
                             sections=[("匹配店铺", ["店铺ID", "店铺", "站点"], rows)])
                cards, provider = [value], "store_clarification"
            else:
                context, data_card = question_context(db, settings, ids[0])
                db.rollback()  # Release warehouse reads before the network request.
                try:
                    analysis = factory(settings).analyze(context, meta["question"])
                    provider = "deepseek"
                except DeepSeekError:
                    analysis, provider = "智能分析暂不可用；以上为已采集数据，缺失项仍待确认。", "unavailable"
                cards = _answer_cards(context, data_card, analysis)
        with session_factory() as db:
            # Recheck scope and route after a possibly slow model request.
            if len(ids) == 1 and ids[0] not in {s["store_id"] for s in health_brief_snapshots(db, settings)["stores"]}:
                raise ValueError("Question store left the allowed scope")
            response_ids = _reply(db, settings, meta["message_id"], meta["chat_id"], cards)
            job = db.get(SyncJob, job_id)
            job.status, job.finished_at = "SUCCEEDED", _utcnow().replace(tzinfo=None)
            job.metadata_json = {**meta, "outbox_ids": response_ids, "analysis_provider": provider}
            db.commit()
            return {"status": "QUEUED", "job_id": job_id, "outbox_ids": response_ids}


def run_question_worker():
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.notifications.worker import process_outbox_batch
    from jx_alert.notifications.outbox import NotificationOutbox
    import logging
    logger = logging.getLogger("jx_alert.bot")
    while True:
        try:
            result = process_one_question(get_settings(), SessionLocal, engine)
            ids = result.get("outbox_ids", [])
            with SessionLocal() as db:
                pending = db.scalars(select(NotificationOutbox.id).where(
                    NotificationOutbox.idempotency_key.startswith("group-question:"),
                    NotificationOutbox.status.in_(["PENDING", "RETRY"])).order_by(NotificationOutbox.id).limit(5)).all()
                ids = list(dict.fromkeys(ids + pending))
            for offset in range(0, len(ids), 5):
                process_outbox_batch(batch_size=5, outbox_ids=ids[offset:offset + 5])
        except JobAlreadyRunning:
            pass
        except Exception as exc:
            logger.error("Question processing failed: %s", type(exc).__name__)
        time.sleep(2)
