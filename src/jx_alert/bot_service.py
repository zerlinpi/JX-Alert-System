"""Durable Feishu messages answered by DeepSeek: free conversation plus SparkX read tools.

The event callback only persists the validated envelope (SyncJob
"feishu_question"); a separate worker thread answers it and replies to the
original message through the outbox. No model or SparkX request runs inside
the event callback. Answered turns keep their text so follow-ups have context.
"""
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import time

from sqlalchemy import func, select

from jx_alert.notifications.cards import simple_card
from jx_alert.notifications.outbox import QUESTION_REPLY_PREFIX, enqueue_notification
from jx_alert.notifications.routing import OPEN_ID, load_routing, report_group_delivery
from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock
from jx_alert.storage.models import SyncJob

_LOG = logging.getLogger("jx_alert.bot")
JOB = "feishu_question"
TITLE = "JX 广告助手"
# Follow-up questions see the recent turns of the same conversation (p2p chat, or one person in the group).
HISTORY_TURNS = 6
HISTORY_WINDOW = timedelta(minutes=60)
MAX_ANSWER_MEMORY = 4000


def _utcnow():
    return datetime.now(timezone.utc)


def _route(settings):
    return report_group_delivery(settings, load_routing(settings.feishu_routing_file), role="group_question")


def _explicit_readers(settings, routing):
    """Leads, group administrators and mapped operators may ask privately."""
    from jx_alert.reports.recipients import ad_lead, company_lead
    from jx_alert.sparkx import stores as directory_module

    readers = set()
    for person in (company_lead(settings, routing), ad_lead(routing)):
        if person:
            readers.add(person["open_id"])
    group = (routing.get("groups") or {}).get(getattr(settings, "feishu_daily_report_chat_id", ""), {})
    readers.update(uid for uid in group.get("admin_open_ids", []) if isinstance(uid, str))
    try:
        directory = directory_module.load(settings)
    except directory_module.StoreDirectoryError:
        directory = {"profiles": {}}
    for entry in directory["profiles"].values():
        readers.update(row["open_id"] for row in entry["operators"])
    return readers


def validate_question_delivery(settings, delivery):
    """Fail closed on removed access; Feishu lookup failures propagate for a later retry."""
    if (not getattr(settings, "feishu_bot_enabled", False) or not settings.delivery_enabled()
            or not isinstance(delivery, dict) or delivery.get("receive_id_type") != "chat_id"):
        return False
    routing = load_routing(settings.feishu_routing_file)
    current = report_group_delivery(settings, routing, role="group_question")
    if delivery.get("role") == "group_question":
        return (delivery.get("receive_id") == current["receive_id"]
                and delivery.get("routing_alias", current.get("routing_alias")) == current.get("routing_alias")
                and delivery.get("routing_changed_at") == current.get("routing_changed_at"))
    if delivery.get("role") != "private_question":
        return False
    chat, sender = delivery.get("receive_id"), delivery.get("sender_open_id")
    if (not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat)
            or any(isinstance(group, dict) and group.get("chat_id") == chat
                   for group in routing.get("groups", {}).values())
            or not isinstance(sender, str) or not OPEN_ID.fullmatch(sender)
            or sender == getattr(settings, "feishu_bot_open_id", "")):
        return False
    if sender in _explicit_readers(settings, routing):
        return True
    from jx_alert.admin_feishu import members
    return sender in members(settings, current["receive_id"])


def _question_delivery(settings, meta):
    if meta.get("chat_type") == "p2p":
        return {"role": "private_question", "receive_id_type": "chat_id", "open_ids": [],
                "receive_id": meta["chat_id"], "sender_open_id": meta["sender_id"],
                "reply_to_message_id": meta["message_id"]}
    saved = meta.get("question_route")
    if not isinstance(saved, dict):
        saved = {**_route(settings), "routing_changed_at": None}
    return {**saved, "role": "group_question", "receive_id": meta["chat_id"],
            "reply_to_message_id": meta["message_id"]}


class QuestionAccessChanged(ValueError):
    """An earlier authorized conversation no longer permits this reply."""


def _reply(db, settings, meta, cards):
    delivery = _question_delivery(settings, meta)
    if not validate_question_delivery(settings, delivery):
        raise QuestionAccessChanged("Original question is outside the current allowed conversation")
    ids = []
    for index, value in enumerate(cards):
        row = enqueue_notification(db, idempotency_key=f"{QUESTION_REPLY_PREFIX}{meta['message_id']}:{index}",
                                   text=value["header"]["title"]["content"], delivery=delivery, card=value)
        ids.append(row.id)
    return ids


def enqueue_group_question(db, settings, event_dict):
    """Persist only validated envelopes; authorize private readers in the worker."""
    if not getattr(settings, "feishu_bot_enabled", False) or not settings.delivery_enabled():
        return {"status": "DISABLED"}
    if not isinstance(event_dict, dict) or event_dict.get("schema") != "2.0":
        return {"status": "IGNORED"}
    header, event = event_dict.get("header", {}), event_dict.get("event", {})
    if not isinstance(header, dict) or not isinstance(event, dict):
        return {"status": "IGNORED"}
    sender, message = event.get("sender", {}), event.get("message", {})
    tenant = getattr(settings, "feishu_bot_tenant_key", "")
    if (header.get("event_type") != "im.message.receive_v1" or header.get("app_id") != settings.feishu_app_id
            or (tenant and header.get("tenant_key") != tenant)
            or not isinstance(sender, dict) or not isinstance(message, dict)
            or sender.get("sender_type") != "user" or message.get("chat_type") not in {"group", "p2p"}
            or message.get("message_type") != "text"):
        return {"status": "IGNORED"}
    kind, chat = message["chat_type"], message.get("chat_id")
    intake_route = _route(settings)
    current_chat = intake_route["receive_id"]
    if (not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat)
            or (kind == "group" and chat != current_chat) or (kind == "p2p" and chat == current_chat)):
        return {"status": "IGNORED"}
    mentions = message.get("mentions") or []
    own_id = getattr(settings, "feishu_bot_open_id", "")
    if not own_id or not isinstance(mentions, list) or (kind == "group" and not any(
            isinstance(m, dict) and isinstance(m.get("id"), dict) and m["id"].get("open_id") == own_id
            for m in mentions)):
        return {"status": "IGNORED"}
    message_id = message.get("message_id")
    sender_id = sender.get("sender_id", {}).get("open_id") if isinstance(sender.get("sender_id"), dict) else None
    if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
        return {"status": "IGNORED"}
    if not isinstance(sender_id, str) or not OPEN_ID.fullmatch(sender_id) or sender_id == own_id:
        return {"status": "IGNORED"}
    try:
        content = json.loads(message.get("content", ""))
        question = content.get("text", "") if isinstance(content, dict) else None
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
        existing = db.scalar(select(SyncJob).where(SyncJob.job_type == JOB,
                                                   SyncJob.metadata_json["message_id"].as_string() == message_id))
        if existing:
            return {"status": "REUSED", "job_id": existing.id}
        pending = db.scalar(select(func.count(SyncJob.id)).where(SyncJob.job_type == JOB,
                                                                 SyncJob.status.in_(["PENDING", "RUNNING"])))
        recent = db.scalar(select(func.count(SyncJob.id)).where(
            SyncJob.job_type == JOB, SyncJob.started_at >= clock - timedelta(minutes=1),
            SyncJob.metadata_json["sender_id"].as_string() == sender_id))
        busy = pending >= getattr(settings, "feishu_bot_max_pending", 50) or recent >= 3
        meta = {"message_id": message_id, "sender_id": sender_id, "chat_id": chat, "chat_type": kind,
                "question": question, "attempts": 0}
        if kind == "group":
            meta["question_route"] = intake_route
        job = SyncJob(job_type=JOB, status="SUCCEEDED" if busy else "PENDING", started_at=clock, metadata_json=meta)
        db.add(job)
        db.flush()
        if busy:
            # No roster API call or data disclosure from the SDK callback; private excess stays silent.
            ids = _reply(db, settings, meta, [simple_card("提问较多", "请稍后重试。")]) if kind == "group" else []
            job.metadata_json = {**meta, "outbox_ids": ids}
            job.finished_at = clock
        db.commit()  # Publish deduplication while the ingestion lock is held.
        return {"status": "BUSY" if busy else "QUEUED", "job_id": job.id}


# ---------------------------------------------------------------- answer rendering

def answer_cards(title, answer, footer):
    """DeepSeek's Markdown answer as at most three reply cards (tables stay tables)."""
    from jx_alert.reports.cards import report_cards

    return report_cards(title, answer, footer, reserve=0)[:3]


def _conversation(meta):
    """Turns of one private chat, or of one person inside the group, belong together."""
    return (meta.get("chat_id"), None if meta.get("chat_type") == "p2p" else meta.get("sender_id"))


def history(db, meta, job_id, now):
    """Recent answered turns of the same conversation, oldest first (no other person's turns)."""
    since = now.replace(tzinfo=None) - HISTORY_WINDOW
    rows = db.scalars(select(SyncJob).where(
        SyncJob.job_type == JOB, SyncJob.status == "SUCCEEDED", SyncJob.id < job_id, SyncJob.started_at >= since,
        SyncJob.metadata_json["chat_id"].as_string() == meta.get("chat_id"))
        .order_by(SyncJob.id.desc()).limit(40)).all()
    turns = []
    for row in rows:
        item = row.metadata_json or {}
        if _conversation(item) == _conversation(meta) and isinstance(item.get("answer_text"), str):
            turns.append({"question": str(item.get("question") or ""), "answer": item["answer_text"]})
        if len(turns) >= HISTORY_TURNS:
            break
    return list(reversed(turns))


# ---------------------------------------------------------------- worker

def _complete(db, settings, job_id, meta, cards, provider):
    job = db.get(SyncJob, job_id)
    try:
        ids = _reply(db, settings, meta, cards)
    except QuestionAccessChanged:
        job.status, job.error_message = "SKIPPED", "会话或读取权限已变更，本次未回复。"
        ids = []
    else:
        job.status = "SUCCEEDED"
    job.finished_at = _utcnow().replace(tzinfo=None)
    job.metadata_json = {**meta, "outbox_ids": ids, "analysis_provider": provider}
    db.commit()
    return {"status": job.status, "job_id": job_id, "outbox_ids": ids}


def _recover_interrupted(db, settings):
    for row in db.scalars(select(SyncJob).where(SyncJob.job_type == JOB, SyncJob.status == "RUNNING")).all():
        meta = row.metadata_json or {}
        if meta.get("attempts", 0) < 2:
            row.status = "PENDING"
        else:
            row.status, row.finished_at = "FAILED", _utcnow().replace(tzinfo=None)


def process_one_question(settings, session_factory, engine, *, answer=None):
    if not getattr(settings, "feishu_bot_enabled", False) or not settings.delivery_enabled():
        return {"status": "DISABLED"}
    from jx_alert.ai.deepseek import DeepSeekError
    from jx_alert.bot_agent import answer_question
    from jx_alert.sparkx.client import SparkXError
    from jx_alert.sparkx.stores import latest_roster

    with database_job_lock(engine, "feishu_question_worker"):
        with session_factory() as db:
            _recover_interrupted(db, settings)
            db.commit()
            job = db.scalar(select(SyncJob).where(SyncJob.job_type == JOB, SyncJob.status == "PENDING")
                            .order_by(SyncJob.id).limit(1))
            if job is None:
                return {"status": "IDLE"}
            now = _utcnow()
            if now.replace(tzinfo=None) - job.started_at > timedelta(minutes=settings.feishu_bot_question_max_age_minutes):
                job.status, job.finished_at = "EXPIRED", now.replace(tzinfo=None)
                db.commit()
                return {"status": "EXPIRED"}
            meta, job_id = dict(job.metadata_json), job.id
            meta["attempts"] = meta.get("attempts", 0) + 1
            job.status, job.metadata_json = "RUNNING", meta
            db.commit()
            if not validate_question_delivery(settings, _question_delivery(settings, meta)):
                job.status, job.finished_at = "SKIPPED", _utcnow().replace(tzinfo=None)
                job.error_message = "会话或读取权限已变更，本次未回复。"
                db.commit()
                return {"status": "SKIPPED", "job_id": job_id}
            turns = history(db, meta, job_id, now)
            roster, _ = latest_roster(db)
        question = meta["question"]
        try:
            result = (answer or answer_question)(settings, question, history=turns, roster=roster)
        except DeepSeekError as exc:
            cards, provider = [simple_card(TITLE, f"DeepSeek 暂时无法回答：{exc}")], "deepseek_unavailable"
        except SparkXError as exc:
            cards, provider = [simple_card(TITLE, f"暂时无法查询 SparkX：{exc.message}")], "sparkx_error"
        else:
            provider = result["provider"]
            footer = (f"数据来源：SparkX MCP 只读查询 {result['calls']} 次 · DeepSeek 分析" if result.get("calls")
                      else None)
            cards = answer_cards(TITLE, result["answer"], footer)
            meta["sparkx_calls"] = result.get("calls", 0)
            meta["answer_text"] = result["answer"][:MAX_ANSWER_MEMORY]
        with session_factory() as db:
            return _complete(db, settings, job_id, meta, cards, provider)


def run_question_worker():
    from jx_alert.config import get_settings
    from jx_alert.notifications.worker import process_outbox_batch
    from jx_alert.storage.db import SessionLocal, engine

    while True:
        try:
            result = process_one_question(get_settings(), SessionLocal, engine)
            ids = list(dict.fromkeys(result.get("outbox_ids", [])))
            for offset in range(0, len(ids), 5):
                process_outbox_batch(batch_size=5, outbox_ids=ids[offset:offset + 5], question_replies_only=True)
            # A standalone bot may be the only reply worker; never touch other producers' rows.
            process_outbox_batch(batch_size=5, question_replies_only=True)
        except JobAlreadyRunning:
            pass
        except Exception as exc:  # noqa: BLE001 - keep the worker alive; never log provider text.
            _LOG.error("Question processing failed: %s", type(exc).__name__)
        time.sleep(2)
