"""Durable group and private questions, with no model request in event callbacks."""
from datetime import date, datetime, timedelta, timezone
import html
import json
import math
import re
import time
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.chat_analysis import _scope
from jx_alert.lingxing.ad_details import DETAIL_LABELS
from jx_alert.notifications.cards import card, number
from jx_alert.notifications.daily_report import build_daily_card
from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.notifications.routing import _load_routing, _route_entry, report_group_delivery
from jx_alert.notifications.store_health import card_fits, health_brief_snapshots
from jx_alert.storage.collection_attempts import collection_issue_for_date as _collection_issue_for_date
from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, Store, SyncJob


def _utcnow():
    return datetime.now(timezone.utc)


def _route(settings):
    return report_group_delivery(settings, _load_routing(settings.feishu_routing_file), role="group_question")


def _explicit_private_readers(settings, routing):
    """Only configured roles confer read access without a membership lookup."""
    groups = routing.get("groups", {})
    configured = [groups.get(settings.feishu_daily_report_chat_id, {}).get("admin_open_ids", [])]
    for section in ("stores", "entities"):
        for key in routing.get(section, {}):
            entry = _route_entry(routing[section], key)
            configured.extend((entry.get("open_ids", []), entry.get("manager_open_ids", [])))
    management = routing.get("management", {})
    if isinstance(management, dict):
        configured.append(management.get("open_ids", []))
    configured.append([item.strip() for item in getattr(settings, "feishu_manager_open_ids", "").split(",")
                       if item.strip()])
    return {uid for items in configured if isinstance(items, list) for uid in items
            if isinstance(uid, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid)}


def validate_question_delivery(settings, delivery):
    """Fail closed on removed access; propagate sanitized lookup failures for retry."""
    if (not getattr(settings, "feishu_bot_enabled", False) or settings.app_dry_run
            or not settings.feishu_enabled or settings.feishu_transport != "app"
            or not isinstance(delivery, dict) or delivery.get("receive_id_type") != "chat_id"):
        return False
    routing = _load_routing(settings.feishu_routing_file)
    current = report_group_delivery(settings, routing, role="group_question")
    if delivery.get("role") == "group_question":
        return (delivery.get("receive_id") == current["receive_id"]
                and delivery.get("routing_alias", current.get("routing_alias")) == current.get("routing_alias")
                and delivery.get("routing_changed_at") == current.get("routing_changed_at"))
    if delivery.get("role") != "private_question":
        return False
    chat, sender = delivery.get("receive_id"), delivery.get("sender_open_id")
    if (not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat)
            or any(group.get("chat_id") == chat for group in routing.get("groups", {}).values())
            or not isinstance(sender, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", sender)
            or sender == getattr(settings, "feishu_bot_open_id", "")):
        return False
    if sender in _explicit_private_readers(settings, routing):
        return True
    from jx_alert.admin_mapping import _members
    # A changed group is checked against its new roster. No forever-trusted
    # membership snapshot or permissive fallback is used on an API failure.
    return sender in _members(settings, current["receive_id"])


def _question_delivery(settings, meta):
    kind = meta.get("chat_type", "group")
    if kind == "p2p":
        return {"role": "private_question", "receive_id_type": "chat_id", "open_ids": [],
                "receive_id": meta["chat_id"], "sender_open_id": meta["sender_id"],
                "reply_to_message_id": meta["message_id"]}
    if kind != "group":
        raise ValueError("Question conversation type is invalid")
    saved = meta.get("question_route")
    if not isinstance(saved, dict):
        # Questions already queued before generation tracking belong to the
        # original route, not to a newer group that happens to reuse its ID.
        saved = {**_route(settings), "routing_changed_at": None}
    return {**saved, "role": "group_question", "receive_id": meta["chat_id"],
            "reply_to_message_id": meta["message_id"]}


class QuestionAccessChanged(ValueError):
    """An earlier authorized conversation no longer permits this reply."""


def _reply(db, settings, message_id, chat_id, cards, *, question_meta=None):
    delivery = _question_delivery(settings, question_meta or {
        "chat_id": chat_id, "message_id": message_id, "chat_type": "group"})
    if not validate_question_delivery(settings, delivery):
        raise QuestionAccessChanged("Original question is outside the current allowed conversation")
    ids = []
    for index, value in enumerate(cards):
        row = enqueue_notification(db, idempotency_key=f"group-question:{message_id}:{index}",
            text=value["header"]["title"]["content"], chat_id=chat_id, card=value,
            delivery=delivery, fixed_route=True)
        ids.append(row.id)
    return ids


def enqueue_group_question(db, settings, event_dict):
    """Persist only validated envelopes; authorize private readers in the worker."""
    if not getattr(settings, "feishu_bot_enabled", False) or settings.app_dry_run or not settings.feishu_enabled:
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
    mentions = message.get("mentions", [])
    if kind == "p2p" and mentions is None:
        mentions = []
    own_id = getattr(settings, "feishu_bot_open_id", "")
    if not own_id or not isinstance(mentions, list) or (kind == "group" and not any(
            isinstance(m, dict) and isinstance(m.get("id"), dict) and m["id"].get("open_id") == own_id
            for m in mentions)):
        return {"status": "IGNORED"}
    message_id = message.get("message_id")
    sender_id = sender.get("sender_id", {}).get("open_id") if isinstance(sender.get("sender_id"), dict) else None
    if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
        return {"status": "IGNORED"}
    if (not isinstance(sender_id, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", sender_id)
            or sender_id == own_id):
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
                "chat_id": chat, "chat_type": kind, "question": question, "attempts": 0})
        if kind == "group":
            job.metadata_json = {**job.metadata_json, "question_route": intake_route}
        db.add(job)
        db.flush()
        if busy:
            value = card("提问较多", subtitle="请稍后重试", sections=[])
            # Do not perform a roster API call or disclose data from the SDK
            # intake callback. Private excess questions remain silent.
            ids = (_reply(db, settings, message_id, chat, [value], question_meta=job.metadata_json)
                   if kind == "group" else [])
            job.metadata_json = {**job.metadata_json, "outbox_ids": ids}
            job.finished_at = clock
        db.commit()  # Publish deduplication while the ingestion lock is held.
        return {"status": "BUSY" if busy else "QUEUED", "job_id": job.id}


_COUNTRY_NAMES = {
    "美国": ("美国", "us", "usa", "united states"),
    "加拿大": ("加拿大", "ca", "canada"),
    "墨西哥": ("墨西哥", "mx", "mexico"),
    "巴西": ("巴西", "br", "brazil"),
    "德国": ("德国", "de", "germany"),
    "西班牙": ("西班牙", "es", "spain"),
}
_NAME_NOISE = {
    "主账号", "子账号", "主账户", "子账户", "账号", "账户", "店铺", "站点", "亚马逊",
    "实时", "采集", "查询", "查看", "分析", "广告", "绩效", "健康", "数据", "情况",
    "订单", "销量", "销售", "进度", "状态", "amazon", "amz", "llc", "inc", "ltd",
    "ctr", "cpc", "cdr", "cvr", "cps", "roas", "cpa", "tcoas", "tacos", "acos", "sku", "asin", "msku",
    *(alias for aliases in _COUNTRY_NAMES.values() for alias in aliases),
}


def _name_text(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _name_parts(value):
    parts = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", _name_text(value))
    result = []
    for part in parts:
        if part.isascii():
            if part not in _NAME_NOISE:
                result.append(part)
        else:
            for noise in sorted((p for p in _NAME_NOISE if not p.isascii()), key=len, reverse=True):
                part = part.replace(noise, " ")
            result.extend(part.split())
    return result


def _country_matches(question, country):
    for label, aliases in _COUNTRY_NAMES.items():
        if _name_text(country) in aliases:
            country = label
            break
    requested = set()
    for label, aliases in _COUNTRY_NAMES.items():
        if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", question)
               if alias.isascii() else alias in question for alias in aliases):
            requested.add(label)
    return not requested or country in requested


def _resolve_store(db, settings, question):
    snapshots = health_brief_snapshots(db, settings)["stores"]
    countries = {s.sid: s.country for s in db.scalars(select(Store).where(Store.sid.in_([s["store_id"] for s in snapshots])))}
    for snapshot in snapshots:
        snapshot["country"] = countries.get(snapshot["store_id"]) or "待确认"
    question = _name_text(question)
    compact = re.sub(r"[\W_]+", "", question)
    exact = []
    for snapshot in snapshots:
        name = snapshot["store_name"]
        key = re.sub(r"[\W_]+", "", _name_text(name or ""))
        if (name != "未同步店铺" and key and _name_parts(name)
                and re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", compact)):
            exact.append((len(key), snapshot["store_id"]))
    if exact:
        longest = max(length for length, _ in exact)
        exact = [sid for length, sid in exact if length == longest]
        return snapshots, [s["store_id"] for s in snapshots if s["store_id"] in exact
                           and (s["country"] == "待确认" or _country_matches(question, s["country"]))]
    # A requested report date is not a store identifier.
    without_dates = re.sub(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?|\d{1,2}月\d{1,2}日", "", question)
    ids = [s["store_id"] for s in snapshots
           if re.search(r"(?<!\d)" + re.escape(s["store_id"]) + r"(?!\d)", without_dates)]
    if not ids:
        parts = _name_parts(question)
        latin = [p for p in parts if p.isascii() and len(p) >= 3 and not p.isdigit()]
        chinese = {p[i:i + 2] for p in parts if not p.isascii() for i in range(len(p) - 1)}
        literal_parts = []
        for literal in re.findall(r"\w+(?:[-_]\w+)+", question):
            components = [p for p in _name_parts(literal) if not p.isdigit() and len(p) >= 2]
            if len(components) >= 2:
                literal_parts.extend(components)
        for snapshot in snapshots:
            name = snapshot["store_name"]
            if not name or name == "未同步店铺":
                continue
            aliases = _name_parts(name)
            aliases.append("".join(p for p in aliases if p.isascii()))
            # Brand fragments are deterministic substring matches, never model
            # guesses. Country/common account terms cannot select a store.
            matched = (all(any(p in alias for alias in aliases if alias.isascii()) for p in latin)
                       if latin else any(p in alias for p in chinese for alias in aliases if not alias.isascii()))
            matched = matched and all(p in _name_text(name) for p in literal_parts)
            if matched and _country_matches(question, snapshot["country"]):
                ids.append(snapshot["store_id"])
    ids = list(dict.fromkeys(ids))
    if len(ids) > 1:
        from jx_alert.bot_intents import operating_snapshots
        operating = {s['store_id'] for s in operating_snapshots(settings, snapshots, question)}
        preferred = [sid for sid in ids if sid in operating]
        if preferred:
            ids = preferred
    return snapshots, ids


def _store_clarification(snapshots, ids, *, collection=False):
    options = [s for s in snapshots if s["store_id"] in ids]
    subtitle = ("匹配到多家店铺，请补充完整店铺名和站点，例如：Geteen 美国。" if options else
                "未匹配到店铺，请提供店铺名或品牌片段及站点，例如：Geteen 美国。")
    return card("请确认要采集的店铺" if collection else "请确认店铺", subtitle=subtitle,
                sections=[("匹配店铺", ["店铺", "站点"],
                           [(s["store_name"], s["country"]) for s in options[:20]])])


_COLLECTION_VERB = r"(?:立即采集|实时采集|重新采集|现在采集|手动采集)"
_COLLECTION_REQUEST = re.compile(
    rf"(?:请\s*)?(?:{_COLLECTION_VERB}\s*(?:店铺\s*)?([1-9][0-9]{{0,17}})\s*(?:店铺)?"
    rf"|([1-9][0-9]{{0,17}})\s*店铺\s*{_COLLECTION_VERB})[。！!]?"
)
_COLLECTION_STATUS = re.compile(r"(?:查询\s*)?采集任务\s*([1-9][0-9]{0,17})\s*状态[。？?]?")
_COLLECTION_NAME_REQUEST = re.compile(
    rf"(?:请\s*)?{_COLLECTION_VERB}\s*([\w\s\-/&()（）]+?)[。！!]?"
)
_COLLECTION_STORE_STATUS = re.compile(r"(?:请\s*)?(?:查询\s*)?(.+?)\s*采集(?:进度|状态)[。？?]?")
_COLLECTION_BLOCK_REASONS = {
    "STORE_OUT_OF_SCOPE": "店铺不在采集范围内",
    "STORE_NOT_SYNCED": "系统尚未同步该店铺",
    "AUTHORIZATION_FAILED": "请先恢复该店铺的领星授权",
    "STORE_UNAVAILABLE": "店铺当前不可采集",
    "SKIPPED_COLLECTION_DISABLED": "该店铺的采集开关已关闭",
    "WAITING_TIMEZONE": "店铺报表时区尚未确认",
    "INVALID_TIMEZONE": "店铺报表时区配置不可用",
}
_COLLECTION_STATES = {
    "PENDING": "等待采集",
    "RUNNING": "采集中",
    "SUCCEEDED": "采集完成",
    "SUCCEEDED_WITH_WARNINGS": "采集完成，部分数据待确认",
    "PARTIAL_FAILED": "部分采集失败",
    "FAILED": "采集失败",
    "BLOCKED": "当前条件无法采集",
    "SKIPPED": "本次未执行采集",
}


def _collection_command(question):
    question = unicodedata.normalize("NFKC", question).strip()
    if re.search(_COLLECTION_VERB + r'|更新|刷新|拉取|重新获取', question) and re.search(
            r"不要|不用|不必|禁止|取消|停止|别|只看|仅查看|不采集|是否|能否|可以吗|"
            r"如何|怎么|说明|例如|示例|演示|假设|引用|写出|输出|解释|什么|意思|吗|为何|是不是|能不能|可不可以", question):
        return None
    natural = re.fullmatch(r'(?:请(?:帮我)?|麻烦(?:帮我)?|帮我)?\s*(?:更新|刷新|拉取|重新获取)'
        r'(?:一下)?\s*(.+?)(?:的)?(?:最新)?数据(?:\s*(?:再|并|然后)(?:做)?(?:分析|看看))?[。！!]?\s*', question)
    if natural:
        return 'name_request', natural[1].strip(' 的')
    status = _COLLECTION_STATUS.fullmatch(question)
    if status:
        return "status", int(status[1])
    request = _COLLECTION_REQUEST.fullmatch(question)
    if request:
        return "request", request[1] or request[2]
    status = _COLLECTION_STORE_STATUS.fullmatch(question)
    if status:
        return "store_status", status[1]
    request = _COLLECTION_NAME_REQUEST.fullmatch(question)
    if request:
        return "name_request", request[1]
    if re.match(rf"(?:请\s*)?{_COLLECTION_VERB}", question):
        return "clarify", None
    return None


def _collection_permission(settings, meta, sid):
    routing = _load_routing(settings.feishu_routing_file)
    group = report_group_delivery(settings, routing, role="group_question")
    if meta.get("chat_type", "group") == "group" and meta["chat_id"] != group["receive_id"]:
        return False
    if meta.get("chat_type", "group") not in {"group", "p2p"}:
        return False
    admins = routing["groups"][settings.feishu_daily_report_chat_id].get("admin_open_ids", [])
    operators = _route_entry(routing.get("stores", {}), sid).get("open_ids", [])
    return meta["sender_id"] in admins or meta["sender_id"] in operators


def _collection_status_card(db, job, sid):
    args = job.metadata_json or {}
    state = _COLLECTION_STATES.get(job.status, "待确认")
    retry_at = args.get("retry_at")
    if job.status == "PENDING" and retry_at:
        try:
            retry = datetime.fromisoformat(retry_at)
            if retry.tzinfo is None:
                retry = retry.replace(tzinfo=timezone.utc)
            state = f"等待重试，最早北京时间 {retry.astimezone(ZoneInfo('Asia/Shanghai')):%m-%d %H:%M:%S}"
        except (TypeError, ValueError):
            state = "等待重试"
    # A follow-up can read the same report day that this job actually completed.
    report_date = (args.get("completed_store_dates") or {}).get(sid)
    subtitle = f"报表日 {report_date}。可继续查询店铺数据。" if report_date else "可发送店铺名加“采集进度”查询。"
    store = db.get(Store, sid)
    return card("采集任务状态", subtitle=subtitle,
                sections=[("任务", ["店铺", "站点", "进度"],
                           [(store.name if store else "未同步店铺", store.country if store else "待确认", state)])])


def _lookup_collection(db, settings, meta, collection_id):
    from jx_alert.operations import CollectionConfigurationError, _checked_scope

    receipt = db.scalar(select(SyncJob).where(
        SyncJob.job_type == "feishu_question", SyncJob.status == "SUCCEEDED",
        SyncJob.metadata_json["chat_id"].as_string() == meta["chat_id"],
        SyncJob.metadata_json["collection_job_id"].as_integer() == collection_id,
        SyncJob.metadata_json["analysis_provider"].as_string() == "collection_request",
    ).order_by(SyncJob.id.desc()).limit(1))
    if (receipt is not None and meta.get("chat_type") == "p2p"
            and (receipt.metadata_json or {}).get("sender_id") != meta["sender_id"]):
        receipt = None
    if receipt is not None:
        sid = receipt.metadata_json.get("collection_store_id")
        job = db.get(SyncJob, collection_id)
        if (sid and job is not None and job.job_type == "manual_collection"
                and (job.metadata_json or {}).get("store_ids") == [sid]
                and _collection_permission(settings, meta, sid)):
            try:
                _checked_scope(settings, [sid])
            except CollectionConfigurationError:
                pass
            else:
                return _collection_status_card(db, job, sid)
    return card("未找到可查询的采集任务", subtitle="请由店铺负责人或管理员查询当前会话发起的采集任务。", sections=[])


def _lookup_store_collection(db, settings, meta, sid):
    receipt = db.scalar(select(SyncJob).where(
        SyncJob.job_type == "feishu_question", SyncJob.status == "SUCCEEDED",
        SyncJob.metadata_json["chat_id"].as_string() == meta["chat_id"],
        SyncJob.metadata_json["collection_store_id"].as_string() == sid,
        SyncJob.metadata_json["analysis_provider"].as_string() == "collection_request",
    ).order_by(SyncJob.id.desc()).limit(1))
    if receipt is not None and type(receipt.metadata_json.get("collection_job_id")) is int:
        return _lookup_collection(db, settings, meta, receipt.metadata_json["collection_job_id"])
    return card("未找到可查询的采集任务", subtitle="当前会话尚未发起该店铺的采集，可发送：实时采集 店铺名。", sections=[])


def _complete_question(db, settings, job_id, meta, cards, provider):
    try:
        if meta.get('reply_store_ids'):
            allowed = {s['store_id'] for s in health_brief_snapshots(db, settings)['stores']}
            if not set(meta['reply_store_ids']) <= allowed:
                raise QuestionAccessChanged('Question stores left the allowed scope')
        response_ids = _reply(db, settings, meta["message_id"], meta["chat_id"], cards, question_meta=meta)
    except QuestionAccessChanged:
        job = db.get(SyncJob, job_id)
        job.status, job.finished_at = "SKIPPED", _utcnow().replace(tzinfo=None)
        job.error_message = "会话或读取权限已变更，本次未回复。"
        db.commit()
        return {"status": "SKIPPED", "job_id": job_id}
    job = db.get(SyncJob, job_id)
    job.status, job.finished_at = "SUCCEEDED", _utcnow().replace(tzinfo=None)
    job.metadata_json = {**meta, "outbox_ids": response_ids, "analysis_provider": provider}
    db.commit()
    return {"status": "QUEUED", "job_id": job_id, "outbox_ids": response_ids}


def _process_collection_command(db, settings, job_id, meta, command):
    """Interpret only explicit user commands; model output cannot initiate work."""
    from jx_alert.operations import CollectionBlocked, enqueue_collection

    action, value = command
    if action == "status":
        reply = _lookup_collection(db, settings, meta, value)
        return _complete_question(db, settings, job_id, meta, [reply], "collection_status")
    if action in {"name_request", "store_status"}:
        snapshots, ids = _resolve_store(db, settings, value)
        multiple = re.search(r"和|以及|还有|及|与|同时|并且|(?<![a-z])and(?![a-z])", _name_text(value))
        if len(ids) != 1 or multiple:
            reply = _store_clarification(snapshots, ids, collection=action == "name_request")
            return _complete_question(db, settings, job_id, meta, [reply], "store_clarification")
        value = ids[0]
        if action == "store_status":
            reply = _lookup_store_collection(db, settings, meta, value)
            return _complete_question(db, settings, job_id, meta, [reply], "collection_status")
    if action == "clarify":
        reply = card("请确认要采集的店铺", subtitle="请提供单个店铺名，例如：实时采集 Geteen 美国。", sections=[])
    elif not _collection_permission(settings, meta, value):
        reply = card("采集请求未受理", subtitle="请由该店铺负责人或群管理员发起采集。", sections=[])
    elif not settings.scheduler_enabled or not settings.mcp_operations_enabled:
        reply = card("采集请求未受理", subtitle="主动采集或调度服务未启用。", sections=[])
    else:
        try:
            with database_job_lock(db.get_bind(), "collection_enqueue"):
                try:
                    result = enqueue_collection(db, store_ids=[value])
                except CollectionBlocked as exc:
                    reply = card("采集请求未受理",
                        subtitle=_COLLECTION_BLOCK_REASONS.get(exc.status, "请检查店铺采集设置。"), sections=[])
                except ValueError:
                    reply = card("采集请求未受理", subtitle="请检查店铺采集设置。", sections=[])
                else:
                    meta.update(collection_job_id=result["job_id"], collection_store_id=value)
                    collection_job = db.get(SyncJob, result["job_id"])
                    reply = _collection_status_card(db, collection_job, value)
                    # Publish the queue entry and the receipt together before
                    # releasing the enqueue lock.
                    return _complete_question(db, settings, job_id, meta, [reply], "collection_request")
        except JobAlreadyRunning:
            reply = card("采集请求繁忙", subtitle="请稍后再试。", sections=[])
    return _complete_question(db, settings, job_id, meta, [reply], "collection_request")


def _ad_totals(db, store_id, on):
    metrics = ("spend", "orders", "units", "sales", "clicks", "impressions")
    observations = db.scalars(select(DailyMetric).where(DailyMetric.store_id == store_id,
        DailyMetric.metric_date == on, DailyMetric.dimension.in_(["campaign_sp", "campaign_sb", "campaign_sd"]))).all()
    coverage = {c.dimension: c for c in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store_id, DataCoverage.metric_date == on))}
    totals = []
    for kind, label in (("sp", "商品推广"), ("sb", "品牌推广"), ("sd", "展示推广")):
        dimension = "campaign_" + kind
        rows = [r for r in observations if r.dimension == dimension]
        entities = {r.entity_id for r in rows}
        record = coverage.get(dimension)
        complete = record is not None and record.status == "COMPLETE" and record.row_count <= len(entities)
        item = {"ad_type": label, "report_date": on.isoformat(), "coverage": record.status if record else "MISSING",
                "no_records": complete and record.row_count == 0 and not rows}
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


class QuestionDateError(ValueError):
    """An ambiguous date needs a user correction rather than a guessed period."""


def _question_date(question, latest, *, timezone_confirmed):
    text = question or ""
    tokens = re.findall(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)", text)
    if not tokens:
        tokens = [(str(latest.year), month, day) for month, day in re.findall(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)]
    try:
        dates = {date(*map(int, token)) for token in tokens}
    except ValueError:
        raise QuestionDateError("日期无效，请使用 YYYY-MM-DD，例如 2026-11-13。") from None
    if len(dates) > 1:
        raise QuestionDateError("本次请指定一个报表日期；系统会同时列出它的前一日对比。")
    if dates:
        return dates.pop()
    # "Today's daily brief" is the delivery day, unlike an explicit intraday query.
    if (re.search(r'(?:今日|今天).{0,4}日报|日报.{0,4}(?:今日|今天)', text)
            and not re.search(r'实时|盘中|截至|截止|未结束', text)):
        return None
    for pattern, offset in ((r"今天|今日|当日", 1), (r"昨天|昨日", 0), (r"前天", -1)):
        if re.search(pattern, text):
            if not timezone_confirmed:
                raise QuestionDateError("店铺报表时区待确认，请使用 YYYY-MM-DD 指定报表日期。")
            return latest + timedelta(days=offset)
    return None


def _question_report_date(db, store, requested, *, explicit):
    """Use one recent complete date only when the default date has no attempt."""
    if store.status != 1:
        return requested, "STORE_UNAVAILABLE", None
    issue = _collection_issue_for_date(db, store.sid, requested)
    if issue == "DAILY_FAILED":
        return requested, "COLLECTION_FAILED", f"查询报表日 {requested} 的采集失败，数据待重新采集。"
    other_warning = (f"报表日 {requested} 的经营与活动日报已提交，其他维度采集未完成，相关明细待复核。"
                     if issue == "OTHER_DIMENSIONS_FAILED" else None)
    if explicit:
        return requested, "EXPLICIT_DATE", other_warning
    if issue is not None:
        return requested, "REQUESTED_DATE", other_warning
    dimensions = {"store", "campaign_sp", "campaign_sb", "campaign_sd"}
    first = requested - timedelta(days=7)
    coverage = db.scalars(select(DataCoverage).where(DataCoverage.store_id == store.sid,
        DataCoverage.metric_date.between(first, requested), DataCoverage.dimension.in_(dimensions))).all()
    if any(r.metric_date == requested for r in coverage) or db.scalar(select(DailyMetric.id).where(
            DailyMetric.store_id == store.sid, DailyMetric.metric_date == requested,
            DailyMetric.dimension.in_(dimensions)).limit(1)) is not None:
        return requested, "REQUESTED_DATE", other_warning
    for offset in range(1, 8):
        candidate = requested - timedelta(days=offset)
        partitions = {r.dimension: r for r in coverage if r.metric_date == candidate}
        if set(partitions) != dimensions or any(r.status != "COMPLETE" for r in partitions.values()):
            continue
        candidate_facts = db.scalars(select(DailyMetric).where(DailyMetric.store_id == store.sid,
            DailyMetric.metric_date == candidate, DailyMetric.dimension.in_(dimensions))).all()
        store_metrics = {r.metric_name for r in candidate_facts if r.dimension == "store"
                         and r.entity_id == store.sid and math.isfinite(r.value) and r.value >= 0}
        if not {"orders", "units", "sales"} <= store_metrics:
            continue
        if any(r.row_count > len({f.entity_id for f in candidate_facts if f.dimension == r.dimension})
               for r in partitions.values()):
            continue
        candidate_issue = _collection_issue_for_date(db, store.sid, candidate)
        if candidate_issue == "DAILY_FAILED":
            continue
        notice = f"查询目标日 {requested} 尚未完成采集；以下展示最近已采集报表日 {candidate}，不代表目标日数据。"
        if candidate_issue == "OTHER_DIMENSIONS_FAILED":
            notice += "其他维度采集未完成，相关明细待复核。"
        return candidate, "LATEST_COMPLETE_DATE", notice
    return requested, "NOT_COLLECTED", f"查询报表日 {requested} 尚未采集；近七日无完整日报可供展示。"


def question_context(db, settings, store_id, *, now=None, question=None):
    clock = now or _utcnow()
    zone = _scope(settings, store_id, now=clock)
    store = db.get(Store, store_id)
    if store is None:
        raise ValueError("Store has not been synchronized")
    completed = date.fromisoformat(zone["report_date"])
    explicit = _question_date(question, completed, timezone_confirmed=zone["timezone_confirmed"])
    requested = explicit or completed
    target, selection, date_warning = _question_report_date(db, store, requested, explicit=explicit is not None)
    authorization_warning = None if store.status == 1 else "店铺授权或启用状态异常，最新经营数据待确认"
    warning = authorization_warning
    if selection == "COLLECTION_FAILED":
        warning = "该报表日最新采集失败，等待重新采集校验"
    if target > completed:
        warning = "查询报表日期尚未结束，不能作为完整日报分析"
        date_warning = (date_warning + " " if date_warning else "") + f"{warning}；最近已结束报表日为 {completed}。"
    data_status = "采集失败" if selection == "COLLECTION_FAILED" else None
    value = build_daily_card(db, store, target, settings=settings, data_warning=warning, data_status=data_status)
    tables = [e for e in value["body"]["elements"] if e["tag"] == "table"]
    tables[0]["columns"][1]["display_name"] = "报表日 " + target.isoformat()
    tables[0]["columns"][2]["display_name"] = "前一日 " + (target - timedelta(days=1)).isoformat()
    if date_warning:
        value["body"]["elements"].insert(1, {"tag": "markdown", "content": html.escape(date_warning, quote=False)})
    kpis = advertising_kpis(db, settings, store_id, target)
    totals = _ad_totals(db, store_id, target)
    if warning:
        for kpi in kpis.values():
            kpi.update(current=None, baseline=None, status="INSUFFICIENT_DATA", change_ratio=None)
        for row in totals:
            row.update({m: None for m in ("spend", "orders", "units", "sales", "clicks", "impressions")})
            row["no_records"] = False
    detail_labels = {**DETAIL_LABELS, "product_ad_sd": "展示推广广告商品"}
    detail_rows = {r.dimension: r for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store_id, DataCoverage.metric_date == target,
        DataCoverage.dimension.in_(detail_labels)))} if not warning else {}
    detail_coverage = []
    for dim, label in detail_labels.items():
        row = detail_rows.get(dim)
        state = (warning if warning else "尚未采集" if row is None else "采集失败" if row.status == "FAILED"
                 else "待核实" if row.status != "COMPLETE" else "接口无记录" if row.row_count == 0 else "采集完成")
        detail_coverage.append({"dataset": label, "status": state,
                                "rows": row.row_count if row and row.status == "COMPLETE" else None})
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
    from jx_alert.analysis.materiality import POLICY
    return {"notification_policy": POLICY if getattr(settings, "alert_materiality_enabled", False) else {},
        "store_id": store_id, "store_name": store.name, "country": store.country,
        "report_date": target.isoformat(), "requested_report_date": requested.isoformat(),
        "date_selection": selection, "report_date_warning": date_warning, "date_basis": zone["date_basis"],
        "last_completed_report_date": completed.isoformat(), "report_date_is_complete": target <= completed,
        "timezone_confirmed": zone["timezone_confirmed"], "authorization_warning": authorization_warning,
        "data_warning": warning,
        "snapshot_at_utc": clock.isoformat(), "sales_and_spend": [
            {"metric": row["c0"], "current_date": target.isoformat(), "current_display": row["c1"],
             "previous_date": (target - timedelta(days=1)).isoformat(), "previous_display": row["c2"],
             "comparison_display": row["c3"]} for row in tables[0]["rows"]],
        "coverage": [{"dataset": row["c0"], "status_label": row["c1"]} for row in tables[1]["rows"]],
        "advertising_totals": totals, "advertising_kpis": kpis,
        "ad_detail_coverage": detail_coverage,
        "health": store_health_snapshot(db, store, settings, now=clock),
        "screening_candidates": screening, "screening_candidate_count": len(screening),
        "campaigns": campaigns, "campaign_count": len(candidates),
        "campaign_selection": "仅当前花费最高的至多10个活动样本，不代表全活动逐一巡检"}, value


def _answer_cards(context, data_card, analysis):
    if context.get('operator_compact'):
        from jx_alert.bot_presentation import operator_card
        cards = [] if context.get('reply_mode') == 'answer' else [operator_card(context)]
        if analysis:
            value = card('店铺回复', subtitle=context['store_name'], sections=[])
            value['body']['elements'].append({'tag': 'markdown', 'content': html.escape(analysis, quote=False)})
            cards.append(value)
        return cards
    # These figures are rendered by the service, not regenerated by the model.
    def display(row, metric):
        if row.get("no_records"):
            return "接口无记录"
        return number(row[metric], money=metric in {"spend", "sales"}, currency=row.get(metric + "_currency") or "")
    cards = [data_card]
    if context.get('question_topic') != 'health':
        totals = [(r["ad_type"], *[display(r, metric) for metric in
                   ("spend", "orders", "units", "sales", "clicks", "impressions")]) for r in context["advertising_totals"]]
        raw = card("广告归因数据", subtitle=context["store_name"] + " · " + context["report_date"],
            sections=[("广告明细", ["广告类型", "花费", "归因订单", "归因销量", "归因销售额", "点击", "曝光"], totals)])
        combined = {**data_card, "body": {"elements": data_card["body"]["elements"] + raw["body"]["elements"][1:]}}
        cards = [combined] if card_fits(combined) else [data_card, raw]
    detail_rows = [(r["dataset"], r["status"], r["rows"] if r["rows"] is not None else "—")
                   for r in context.get("ad_detail_coverage", [])]
    if detail_rows:
        detail_card = card("品牌与展示推广接口", subtitle=context["report_date"],
                           sections=[("明细采集状态", ["报表", "状态", "源记录数"], detail_rows)])
        combined_detail = {**cards[-1], "body": {"elements": (cards[-1]["body"]["elements"]
                           + detail_card["body"]["elements"][1:])}}
        if card_fits(combined_detail):
            cards[-1] = combined_detail
        else:
            cards.append(detail_card)
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
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError, analysis_cache_key
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
            if not validate_question_delivery(settings, _question_delivery(settings, meta)):
                job.status, job.finished_at = "SKIPPED", _utcnow().replace(tzinfo=None)
                job.error_message = "会话或读取权限已变更，本次未回复。"
                db.commit()
                return {"status": "SKIPPED", "job_id": job_id}
            collection = _collection_command(meta["question"])
            if collection is not None:
                return _process_collection_command(db, settings, job_id, meta, collection)
            snapshots, ids = _resolve_store(db, settings, meta["question"])
            from jx_alert.bot_intents import global_question_cards, focus_question
            global_cards = global_question_cards(db, settings, meta['question'], snapshots, ids, now=_utcnow())
            if global_cards is not None:
                meta['reply_store_ids'] = [s['store_id'] for s in snapshots]
                return _complete_question(db, settings, job_id, meta, global_cards, 'scoped_store_query')
            if not ids and re.fullmatch(r'(?:你好|您好|在吗|你能做什么|有什么功能|怎么使用)[？?。!！\s]*', meta['question']):
                value = card('使用机器人', subtitle='可以直接用自己的话提问。', sections=[], notes=[
                    '例如：现在有哪些店铺数据、Geteen美国是否有违规、解释这家店的CPC，或请同步Geteen美国最新数据。'])
                return _complete_question(db, settings, job_id, meta, [value], 'question_help')
            if not ids and not re.search(r'\d{4,}|[A-Za-z]{3,}', re.sub(r'(?<![A-Za-z0-9])(?:CTR|CPC|CDR|CVR|CPS|ROAS|CPA|TCOAS|TACOS)(?![A-Za-z0-9])', '', meta['question'], flags=re.I)):
                from jx_alert.bot_intents import operating_snapshots
                from jx_alert.bot_presentation import general_context
                allowed = operating_snapshots(settings, snapshots, meta['question'])
                meta['reply_store_ids'] = [s['store_id'] for s in allowed]
                context = general_context(allowed)
                db.rollback()
                try:
                    analysis = factory(settings).analyze(context, meta['question'])
                    provider = 'deepseek_general'
                except DeepSeekError:
                    analysis, provider = '智能问答暂不可用，请稍后重试。', 'unavailable'
                value = card('机器人回复', subtitle='', sections=[], notes=[analysis])
                return _complete_question(db, settings, job_id, meta, [value], provider)
            if len(ids) != 1:
                value = _store_clarification(snapshots, ids)
                cards, provider = [value], "store_clarification"
            else:
                try:
                    context, data_card = question_context(db, settings, ids[0], question=meta["question"])
                    context, data_card = focus_question(context, data_card, meta['question'])
                except QuestionDateError as exc:
                    cards = [card("请确认报表日期", subtitle=str(exc), sections=[])]
                    provider = "date_clarification"
                else:
                    meta.update({key: context[key] for key in ("report_date", "requested_report_date", "date_selection")})
                    try:
                        cache_key = analysis_cache_key(context, meta["question"], settings.deepseek_model)
                    except DeepSeekError:
                        # Preserve the ordinary data-card fallback when model
                        # input is too large or rejected; never cache this case.
                        cache_key = None
                    cached = db.scalar(select(SyncJob).where(SyncJob.job_type == "feishu_question",
                        SyncJob.status == "SUCCEEDED", SyncJob.finished_at >= _utcnow().replace(tzinfo=None) - timedelta(minutes=10),
                        SyncJob.metadata_json["chat_id"].as_string() == meta["chat_id"],
                        SyncJob.metadata_json["sender_id"].as_string() == meta["sender_id"],
                        SyncJob.metadata_json["analysis_cache_key"].as_string() == cache_key,
                        SyncJob.metadata_json["analysis_provider"].as_string().in_(["deepseek", "deepseek_cache"]))
                        .order_by(SyncJob.id.desc()).limit(1)) if cache_key else None
                    cached_text = (cached.metadata_json or {}).get("analysis_text") if cached else None
                    cached_at = (cached.metadata_json or {}).get("analysis_created_at") if cached else None
                    if not isinstance(cached_at, str) or cached_at < (_utcnow() - timedelta(minutes=10)).isoformat():
                        cached_text = None
                    db.rollback()  # Release warehouse reads before the network request.
                    try:
                        if isinstance(cached_text, str) and cached_text:
                            analysis, provider = cached_text, "deepseek_cache"
                        else:
                            client = factory(settings)
                            analysis = client.analyze(context, meta["question"])
                            provider = "deepseek"
                            meta["model_usage"] = getattr(client, "last_usage", {})
                        meta.update(analysis_cache_key=cache_key, analysis_text=analysis,
                            analysis_created_at=cached_at if provider == "deepseek_cache" else _utcnow().isoformat())
                    except DeepSeekError:
                        analysis, provider = ("智能分析暂不可用，请稍后重试。" if context.get('reply_mode') == 'answer' else
                            "智能分析暂不可用；以上为已采集数据，缺失项仍待确认。"), "unavailable"
                    cards = _answer_cards(context, data_card, analysis)
        with session_factory() as db:
            # Recheck scope and route after a possibly slow model request.
            if len(ids) == 1 and ids[0] not in {s["store_id"] for s in health_brief_snapshots(db, settings)["stores"]}:
                raise ValueError("Question store left the allowed scope")
            return _complete_question(db, settings, job_id, meta, cards, provider)


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
