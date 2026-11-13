"""Chat-reviewed KPI notifications with immutable observations and fixed routing."""
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import select

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.chat_analysis import _chinese, _now, _scope
from jx_alert.notifications.cards import AD_KPI_HEADERS, ad_kpi_rows, card
from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.notifications.routing import _load_routing, _routed_open_ids, report_group_delivery
from jx_alert.storage.models import AdProduct, Campaign, Store, SyncJob


def _policy(settings):
    return {
        "ratio": settings.alert_kpi_change_ratio, "min_impressions": settings.alert_kpi_min_impressions,
        "min_clicks": settings.alert_min_clicks, "min_spend": settings.alert_min_spend,
        "group": settings.feishu_daily_report_chat_id,
        "group_fallback": settings.feishu_group_fallback_enabled, "version": 1,
    }


def _ready_store(db, settings, store_id, clock):
    zone = _scope(settings, str(store_id), now=clock)
    store = db.get(Store, str(store_id))
    if store is None or store.status != 1 or store.timezone != zone["timezone"]:
        raise ValueError("Store authorization or timezone is not ready")
    return store, zone


def _object_details(db, store, dimension, entity_id):
    """Use the same master data for the frozen review and its send-time check."""
    object_name, state = store.name, None
    if dimension.startswith("campaign_"):
        campaign = db.scalar(select(Campaign).where(Campaign.store_id == store.sid,
            Campaign.campaign_type == dimension.removeprefix("campaign_"),
            Campaign.campaign_id == str(entity_id)))
        if campaign:
            object_name, state = campaign.name or str(entity_id), campaign.state
    if dimension.startswith("product_ad_"):
        product = db.scalar(select(AdProduct).where(AdProduct.store_id == store.sid,
            AdProduct.campaign_type == dimension.removeprefix("product_ad_"),
            AdProduct.ad_id == str(entity_id)))
        if product:
            object_name, state = " / ".join(str(v) for v in (product.seller_sku, product.asin, entity_id) if v), product.state
    return object_name, state


def prepare_kpi_review(db, settings, store_id, *, dimension="store", entity_id=None, now=None):
    clock = _now(now)
    store, zone = _ready_store(db, settings, store_id, clock)
    comparisons = advertising_kpis(db, settings, str(store_id), zone["report_date"],
                                   dimension=dimension, entity_id=entity_id)
    object_name, state = _object_details(db, store, dimension, entity_id)
    context = {
        "store_id": str(store_id), "store_name": store.name, "dimension": dimension,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "object_name": object_name, "current_state": state, "report_date": zone["report_date"],
        "timezone": zone["timezone"], "date_basis": zone["date_basis"],
        "policy": _policy(settings), "kpis": comparisons,
        "instructions": "逐项分析kpis。仅ADVERSE可提交中文结论，结合样本量、活动状态和经营背景复核。"
            "CTR和CPC用已完成日报，CDR、CPS、ROAS、CPA用成熟归因日。TCOAS仅店铺全部广告汇总适用。"
            "参考值由同对象七日分子分母汇总后相除，七日缺失时用精确前一日。"
            "数据不足、分母为零、币种不一致和样本不足均保留状态。无异常提交空数组，群内静默。",
    }
    job = SyncJob(job_type="chat_ad_kpi_analysis", status="AWAITING_REVIEW",
        store_count=1, record_count=sum(e["evidence_count"] for e in comparisons.values()),
        started_at=clock.replace(tzinfo=None),
        metadata_json={"context": context, "expires_at": (clock + timedelta(hours=2)).isoformat()})
    db.add(job)
    db.flush()
    return {"review_id": job.id, **context}


def submit_kpi_review(db, settings, review_id, findings, *, now=None):
    clock = _now(now)
    job = db.scalar(select(SyncJob).where(SyncJob.id == review_id,
        SyncJob.job_type == "chat_ad_kpi_analysis").with_for_update())
    if job is None:
        raise ValueError("Unknown advertising KPI review")
    meta = job.metadata_json
    if job.status == "REVIEWED":
        if findings != meta["findings"]:
            raise ValueError("A completed KPI review cannot be changed")
        return meta["result"]
    if clock > datetime.fromisoformat(meta["expires_at"]):
        raise ValueError("KPI review expired; fetch a fresh snapshot")
    context = meta["context"]
    store, zone = _ready_store(db, settings, context["store_id"], clock)
    if (context["policy"] != _policy(settings) or context["report_date"] != zone["report_date"]
            or context["timezone"] != zone["timezone"]):
        raise ValueError("KPI policy or reporting date changed; fetch a fresh snapshot")
    details = _object_details(db, store, context["dimension"], context["entity_id"])
    if store.name != context["store_name"] or details != (context["object_name"], context["current_state"]):
        raise ValueError("Advertising object name or state changed; fetch a fresh snapshot")
    if not isinstance(findings, list) or len(findings) > 7:
        raise ValueError("Submit up to seven KPI findings")
    live = advertising_kpis(db, settings, store.sid, zone["report_date"],
        dimension=context["dimension"], entity_id=context["entity_id"])
    if live != context["kpis"]:
        raise ValueError("KPI source observations changed; fetch a fresh snapshot")
    proposed, seen = [], set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"metric", "summary", "action"}:
            raise ValueError("KPI findings require metric, summary and action")
        metric = finding["metric"]
        if metric not in live or metric in seen:
            raise ValueError("Use each supported KPI at most once")
        seen.add(metric)
        summary, action = _chinese(finding["summary"], 160), _chinese(finding["action"], 100)
        if live[metric]["status"] != "ADVERSE":
            continue
        proposed.append((metric, summary, action))
    routing = _load_routing(settings.feishu_routing_file) if proposed else {}
    ids, titles = [], []
    for metric, summary, action in proposed:
        delivery = report_group_delivery(settings, routing, role="chat_ad_kpi")
        entity_id = context["entity_id"] or store.sid
        group = routing["groups"][settings.feishu_daily_report_chat_id]
        owners = _routed_open_ids(routing, store.sid, entity_id)
        admins = group.get("admin_open_ids", [])
        if not owners and not settings.feishu_group_fallback_enabled:
            raise ValueError("Verified operator mapping is required")
        if owners and admins:
            delivery["open_ids"] = list(dict.fromkeys(owners + admins))
        title = live[metric]["title"]
        state = (context.get("current_state") or "").lower()
        subtitle = store.name + "（" + store.sid + "） · " + context["object_name"]
        if state in {"paused", "archived"}:
            subtitle += "（当前未投放）"
        value = card(title, subtitle=subtitle,
            sections=[("发现", ["结论", "建议"], [(summary, action)]),
                      ("广告指标", AD_KPI_HEADERS, ad_kpi_rows(live))])
        identity = [store.sid, context["dimension"], entity_id, metric, live[metric]["current_date"], delivery["receive_id"]]
        key = "chat-ad-kpi:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        row = enqueue_notification(db, idempotency_key=key, text=title + "：" + summary,
            store_id=store.sid, entity_id=entity_id, chat_id=delivery["receive_id"],
            card=value, delivery=delivery, fixed_route=True)
        ids.append(row.id)
        titles.append(row.payload["card"]["header"]["title"]["content"])
    result = {"status": "QUEUED" if ids else "NO_ADVERSE_KPI",
              "outbox_ids": ids, "notification_titles": titles,
              "kpi_statuses": {k: v["status"] for k, v in live.items()}}
    job.metadata_json = {**meta, "findings": findings, "result": result}
    job.status, job.finished_at = "REVIEWED", clock.replace(tzinfo=None)
    db.flush()
    return result
