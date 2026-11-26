"""Queue the lanes into the durable outbox and gate every later send.

daily   08:30 Beijing: DeepSeek queries SparkX and writes the daily report → report group (@ the
        operators of the stores it flags) + 总负责人
review  08:30 Beijing: DeepSeek's 7-day campaign anomaly review → report group (@ the affected stores'
        operators once mapped) + 广告负责人; optional private copies per operator
hourly  every hour inside the owner's window: wasteful/opportunity campaigns today, found by
        DeepSeek and checked against the SparkX rows → report group (@ operators) + 广告负责人;
        each campaign/category once per marketplace day
test    the owner's test send of a console preview (jx:test:… keys, receipt jx_test): the same
        cards and Excel, with no 测试 wording; never counts as a lane run or as an hourly alert

All lanes are on by default and still notify the group when no operator is mapped.
Cards are frozen at queue time; before each POST the worker re-checks the switch,
the window and the exact recipient. The group copy @-mentions the operators of the
affected stores; a lead's private copy names them in a first line instead (「对应运营：
高爽（Campx）…；未映射运营：…」, owner Nov 2026). Each card message is followed by its
Excel copy (owner, Nov 2026): the whole message on its first sheet, dated, saved under
data/reports and sent to exactly the recipients of that message (transport ``app_file``).
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select

from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.reports import archive, cards, recipients, tasks, workbook
from jx_alert.reports import settings as lane_settings
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import SparkXError

_LOG = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
FILE_TRANSPORT = "app_file"
OPENS, GENERATE_UNTIL, SEND_UNTIL = time(8, 30), time(10, 0), time(12, 0)
HOURLY_TTL = timedelta(minutes=50)
TEST_TTL = timedelta(minutes=30)
PREFIX = "jx:"
LANES = ("daily", "review", "hourly")
JOBS = {lane: f"jx_{lane}" for lane in LANES}
TEST_JOB = "jx_test"
_TERMINAL_OK = {"SUCCEEDED"}


def _utc(now):
    return now.astimezone(timezone.utc).replace(tzinfo=None)


def is_lane(key, payload):
    return isinstance(key, str) and key.startswith(PREFIX) and payload.get("jx_lane") in LANES


def _key(lane, slot, delivery, part=0):
    digest = hashlib.sha256(json.dumps([delivery["role"], delivery["receive_id"]]).encode()).hexdigest()[:24]
    return f"{PREFIX}{lane}:{slot}:{digest}" + (f":{part}" if part else "")


def is_test(key, payload):
    return payload.get("jx_test") is True and isinstance(key, str) and key.startswith(
        f"{PREFIX}test:{payload.get('jx_lane')}:")


def delivery_window(key, payload, now):
    """READY / WAITING (with due UTC) / EXPIRED, from the frozen payload only."""
    instant = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    lane = payload.get("jx_lane")
    if payload.get("jx_test") is True:
        # A console test send goes out right away, or not at all after 30 minutes.
        try:
            created = datetime.fromisoformat(payload["jx_created_at"])
            if created.tzinfo is None or lane not in LANES or not is_test(key, payload):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return "EXPIRED", None
        return ("READY", None) if timedelta() <= instant - created <= TEST_TTL else ("EXPIRED", None)
    if lane == "hourly":
        try:
            created = datetime.fromisoformat(payload["jx_created_at"])
            if created.tzinfo is None or not key.startswith(f"{PREFIX}hourly:{payload['jx_slot']}:"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return "EXPIRED", None
        return ("READY", None) if timedelta() <= instant - created <= HOURLY_TTL else ("EXPIRED", None)
    try:
        scheduled = date.fromisoformat(payload["jx_day"])
        if lane not in {"daily", "review"} or not key.startswith(f"{PREFIX}{lane}:{scheduled.isoformat()}:"):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return "EXPIRED", None
    local = instant.astimezone(BEIJING)
    opens = datetime.combine(scheduled, OPENS, BEIJING)
    if local < opens:
        return "WAITING", opens.astimezone(timezone.utc).replace(tzinfo=None)
    if local.date() != scheduled or local.time() >= SEND_UNTIL:
        return "EXPIRED", None
    return "READY", None


def delivery_blocked(settings, key, payload, now):
    """A reason to keep the record unsent, or None when this exact send is still wanted."""
    lane = payload.get("jx_lane")
    if payload.get("jx_test") is True:
        # The owner chose this recipient for this test; the lane's switches do not apply.
        if delivery_window(key, payload, now)[0] != "READY":
            return "测试消息已过发送时段，保留记录不补发"
        return recipients.delivery_current(settings, lane, payload.get("delivery") or {})
    try:
        config = lane_settings.load(settings)["lanes"][lane]
    except (lane_settings.SettingsError, KeyError):
        return "通知设置无法核验，保留记录不补发"
    if not config["enabled"]:
        return "该通知已停用，保留记录不补发"
    if delivery_window(key, payload, now)[0] != "READY":
        return "已过发送时段，保留记录不补发"
    delivery = payload.get("delivery") or {}
    kind = (delivery.get("role") or "").rsplit("_", 1)[-1]
    flag = {"group": "to_group", "lead": "to_lead", "operator": "to_operators"}.get(kind)
    if flag is None or not config.get(flag):
        return "该发送对象已关闭，保留记录不补发"
    return recipients.delivery_current(settings, lane, delivery)


def mention_plan(settings, payload):
    """(open_ids, {open_id: their stores}) to @ in this send.

    Only group copies mention anyone: the frozen operators that are still mapped and
    still in the group, while the owner keeps 「群内 @ 对应运营」 on (a test send
    mentions only when the owner asked for it). Private messages mention no one.
    """
    delivery = payload.get("delivery") or {}
    lane = payload.get("jx_lane")
    if delivery.get("role") != recipients.role(lane, "group") or not delivery.get("open_ids"):
        return [], {}
    if payload.get("jx_test") is True:
        wanted = payload.get("jx_test_mentions") is True
    else:
        try:
            wanted = bool(lane_settings.load(settings)["lanes"][lane].get("mention_operators"))
        except (lane_settings.SettingsError, KeyError):
            wanted = False
    if not wanted:
        return [], {}
    entries = recipients.current_mention_stores(settings, delivery)
    return [uid for uid, _ in entries], recipients.mention_labels(delivery, entries)


def send_mentions(settings, payload):
    """The open_ids of mention_plan."""
    return mention_plan(settings, payload)[0]


def sparkx_ready(settings):
    from jx_alert.sparkx.credentials import effective_token

    return bool(effective_token(settings))


def deepseek_ready(settings):
    from jx_alert.ai import deepseek_config

    try:
        return deepseek_config.configuration(settings)["configured"]
    except deepseek_config.DeepSeekConfigError:
        return False


# ---------------------------------------------------------------- run receipts

def _jobs(db, lane, field, value):
    from jx_alert.storage.models import SyncJob

    return list(db.scalars(select(SyncJob).where(
        SyncJob.job_type == JOBS[lane], SyncJob.metadata_json[field].as_string() == value)
        .order_by(SyncJob.id.desc())))


def _receipt(db, lane, status, now, **metadata):
    from jx_alert.storage.models import SyncJob

    instant = _utc(now)
    job = SyncJob(job_type=JOBS[lane], status=status, started_at=instant, finished_at=instant,
                  store_count=metadata.pop("store_count", 0), metadata_json=metadata)
    db.add(job)
    db.flush()
    return job


def _retry_later(jobs, now, *, limit, pause=timedelta(minutes=10)):
    failures = [job for job in jobs if job.status == "FAILED"]
    return len(failures) >= limit or bool(failures and failures[0].started_at
                                          and _utc(now) - failures[0].started_at < pause)


def _enqueue(db, lane, slot, delivery, card, now, extra, part=0, *, scope=None):
    from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification

    key = _key(scope or lane, slot, delivery, part)
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)) is not None:
        return None  # Never rewrite a frozen card for the same slot and recipient.
    row = enqueue_notification(db, idempotency_key=key, text=card["header"]["title"]["content"],
                               delivery=delivery, card=card, extra={
                                   "jx_lane": lane, "jx_created_at": now.astimezone(timezone.utc).isoformat(),
                                   **extra})
    return row.id


def _enqueue_cards(db, lane, slot, delivery, values, now, extra, *, scope=None):
    ids = []
    for part, value in enumerate(values):
        # Only the first card of a group message carries the @-mentions.
        target = delivery if part == 0 else {**delivery, "open_ids": [], "mentions": {}, "mention_names": {}}
        row_id = _enqueue(db, lane, slot, target, value, now, extra, part, scope=scope)
        ids += [row_id] if row_id else []
    return ids


def _enqueue_file(db, lane, slot, delivery, attachment, now, extra, *, scope=None):
    """The Excel copy for the recipient of a card just queued: no @-mention, never rewritten."""
    from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification

    if attachment is None:
        return []
    key = _key(scope or lane, slot, delivery, "file")
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)) is not None:
        return []
    target = {**delivery, "open_ids": [], "mentions": {}, "mention_names": {}}
    row = enqueue_notification(db, idempotency_key=key, text="Excel：" + attachment["name"], delivery=target,
                               extra={"jx_lane": lane, "jx_created_at": now.astimezone(timezone.utc).isoformat(),
                                      **extra, "transport": FILE_TRANSPORT, "file": dict(attachment)})
    return [row.id]


def _queue_message(db, lane, slot, delivery, values, attachment, now, extra, *, scope=None):
    """Cards, then their Excel copy (only when the cards were queued now)."""
    ids = _enqueue_cards(db, lane, slot, delivery, values, now, extra, scope=scope)
    return ids + (_enqueue_file(db, lane, slot, delivery, attachment, now, extra, scope=scope) if ids else [])


# ---------------------------------------------------------------- Excel copies

def _responsible_line(responsible):
    """The plain 「对应运营：…」 line for an Excel copy, or nothing."""
    line = cards.operator_line(*responsible, markup=False) if responsible else ""
    return [line] if line else []


def report_book(lane, result, text, now, *, title, responsible=None):
    """(file base name, .xlsx bytes) of a daily report or review text.

    ``responsible`` (recipients.responsible of the stores the report flags) adds the
    「对应运营」 line, so the file names whom each flagged store belongs to.
    """
    if lane == "daily":
        day = date.fromisoformat(result["report_date"])
        return workbook.daily_name(result["report_date"]), workbook.report_book(
            title, text, period_label="统计日（美西）", period=result["report_date"],
            details=[("对比日", (day - timedelta(days=1)).isoformat()), *_responsible_line(responsible)],
            created=now)
    start, end = (date.fromisoformat(value) for value in result["window"])
    before = f"{(start - timedelta(days=7)).isoformat()} ~ {(end - timedelta(days=7)).isoformat()}"
    return workbook.review_name(result["window"]), workbook.report_book(
        title, text, period_label="近 7 日（美西）", period=f"{start.isoformat()} ~ {end.isoformat()}",
        details=[("对比前 7 日", before), *_responsible_line(responsible)], created=now)


def alerts_book(result, findings, names, operators, now, *, scoped=False, responsible=None):
    """(file base name, .xlsx bytes) of hourly alerts."""
    return workbook.hourly_name(result["day"], result["time"]), workbook.alerts_book(
        cards.hourly_title(result["day"], result["time"]), cards.hourly_lead(findings, scoped=scoped), findings,
        names=names, operators=operators, period=f"{result['day']} {result['time']}",
        details=_responsible_line(responsible), created=now)


def _for(delivery, lane, values, lead_values):
    """Private lead copies carry the 「对应运营」 line (owner, Nov 2026); other copies are unchanged."""
    return lead_values if delivery.get("role") == recipients.role(lane, "lead") else values


def save_book(settings, folder, book, now, *, who=None):
    """The archive record of a workbook (see reports.archive), or None when it cannot be built or saved.

    A missing Excel copy never stops the message itself.
    """
    if book is None:
        return None
    try:
        base, data = book() if callable(book) else book
        return archive.save(settings, folder, workbook.file_name(base, who=who), data, now=now)
    except (archive.ArchiveError, ValueError, KeyError, TypeError) as exc:
        _LOG.warning("Excel copy not saved for %s (%s)", folder, type(exc).__name__)
        return None


def _remember(db, now):
    def remember(roster):
        try:
            with db.begin_nested():
                directory_module.record_roster(db, roster, now)
        except Exception:  # noqa: BLE001 - a snapshot must never fail the lane.
            pass
    return remember


def _failure(exc):
    if isinstance(exc, SparkXError):
        return {"error": exc.code, "message": exc.message}
    if isinstance(exc, DeepSeekError):
        return {"error": "DEEPSEEK_FAILED", "message": str(exc)[:200]}
    return {"error": "AGENT_OUTPUT_INVALID", "message": "DeepSeek 的输出不符合约定格式，稍后重试。"}


def _run(settings, lane, config, *, now, directory, db, runner, alerted=frozenset()):
    return (runner or tasks.run_lane)(settings, lane, config, now=now, directory=directory, alerted=alerted,
                                      on_roster=_remember(db, now))


def _summary(result):
    summary = result["summary"]
    return {"sparkx_calls": summary.get("sparkx_calls"), "skill_reads": summary.get("skill_reads"),
            "rounds": summary.get("rounds"), "model": summary.get("model"), "usage": summary.get("usage"),
            "skills": result.get("skills")}


def _title(lane, result):
    if lane == "daily":
        return f"广告日报 · {result['report_date']}"
    start, end = result["window"]
    return f"广告异常复盘 · {start[5:]}~{end[5:]} 对比前 7 日"


# ---------------------------------------------------------------- 08:30 lanes

def _morning_plan(settings, routing_data, lane, config):
    plans = []
    if config["to_group"] and (group := recipients.group_delivery(settings, routing_data, lane)):
        plans.append(group)
    if config["to_lead"]:
        plans += recipients.lead_deliveries(settings, routing_data, lane)
    return plans


def _mention_operators(plans, settings, routing_data, directory, config, lane, pids, names):
    """The group copy @-mentions the mapped operators of these stores; nobody else changes."""
    if not config.get("mention_operators"):
        return plans
    mentions = recipients.operator_mentions(settings, routing_data, directory, pids)
    group = recipients.role(lane, "group")
    return [recipients.with_operator_mentions(plan, mentions, names) if plan["role"] == group and mentions else plan
            for plan in plans]


def _names(result):
    roster = result.get("roster") or {}
    return directory_module.display_names(roster) if roster else {}


def _gate(settings, config, now):
    if not config["enabled"]:
        return "DISABLED"
    if not settings.delivery_enabled():
        return "DELIVERY_DISABLED"
    if not sparkx_ready(settings):
        return "SPARKX_NOT_CONFIGURED"  # No receipt: nothing to retry until a token is saved.
    if not deepseek_ready(settings):
        return "DEEPSEEK_NOT_CONFIGURED"
    return None


def queue_morning_lane(db, settings, lane, *, now, runner=None, scoper=None):
    """Run DeepSeek once, freeze the cards and enqueue them per recipient for today's Beijing date."""
    if lane not in {"daily", "review"}:
        raise ValueError("Unknown morning lane")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Lanes require a timezone-aware clock")
    config = lane_settings.load(settings)["lanes"][lane]
    local = now.astimezone(BEIJING)
    if not config["enabled"]:
        return {"status": "DISABLED"}
    if not OPENS <= local.time() < GENERATE_UNTIL:
        return {"status": "WAITING"}
    if lane == "review" and config["weekly"] and local.weekday() != 0:
        return {"status": "NOT_SCHEDULED_TODAY"}
    if blocked := _gate(settings, config, now):
        return {"status": blocked}
    day = local.date().isoformat()
    jobs = _jobs(db, lane, "day", day)
    if any(job.status in _TERMINAL_OK for job in jobs):
        return {"status": "ALREADY_COMPLETE"}
    if _retry_later(jobs, now, limit=6):
        return {"status": "RETRY_LATER"}
    routing_data = recipients.routing(settings)
    plans = _morning_plan(settings, routing_data, lane, config)
    directory = directory_module.load(settings)
    if not plans and not (lane == "review" and config["to_operators"]):
        if not any(job.status == "SKIPPED" for job in jobs):
            _receipt(db, lane, "SKIPPED", now, day=day, reason="NO_RECIPIENT")
        return {"status": "NO_RECIPIENT"}
    try:
        result = _run(settings, lane, config, now=now, directory=directory, db=db, runner=runner)
    except (SparkXError, DeepSeekError, ValueError) as exc:
        failure = _failure(exc)
        _receipt(db, lane, "FAILED", now, day=day, **failure)
        return {"status": "FAILED", **failure}
    color = "blue" if lane == "daily" else ("orange" if result["stores"] else "green")
    title = _title(lane, result)
    values = cards.report_cards(title, result["content"], color=color)
    names = _names(result)
    responsible = recipients.responsible(directory, set(result["stores"]), names)
    lead_values = cards.with_operator_line(values, cards.operator_line(*responsible))
    attachment = save_book(settings, lane, lambda: report_book(lane, result, result["content"], now, title=title,
                                                               responsible=responsible), now)
    files = [attachment["name"]] if attachment else []
    extra = {"jx_day": day, "jx_profiles": result["profiles"]}
    plans = _mention_operators(plans, settings, routing_data, directory, config, lane, set(result["stores"]), names)
    ids = []
    for delivery in plans:
        ids += _queue_message(db, lane, day, delivery, _for(delivery, lane, values, lead_values), attachment, now,
                              extra)
    scoped = 0
    if lane == "review" and config["to_operators"] and result["stores"]:
        leads = {plan["receive_id"] for plan in plans if plan["receive_id_type"] == "open_id"}
        for delivery in recipients.operator_deliveries(settings, routing_data, directory, lane,
                                                      set(result["stores"]), exclude=leads):
            stores = [names.get(key, key) for key in delivery["profiles"]]
            try:
                text = (scoper or tasks.scope_report)(settings, result["content"], stores)
            except DeepSeekError:
                text = None
            if not text:
                continue
            scoped += 1
            own_title = title + " · 你负责的店铺"
            own = save_book(settings, lane, lambda: report_book(lane, result, text, now, title=own_title), now,
                            who=delivery["name"])
            files += [own["name"]] if own else []
            ids += _queue_message(db, lane, day, delivery, cards.report_cards(own_title, text, color=color), own, now,
                                  {**extra, "jx_profiles": delivery["profiles"]})
    db.flush()
    _receipt(db, lane, "SUCCEEDED", now, day=day, outbox_ids=ids, cards=len(values), stores=result["stores"],
             report_date=result["report_date"], window=result["window"], operator_copies=scoped, files=files,
             store_count=len(result["profiles"]), **_summary(result))
    return {"status": "QUEUED", "outbox_ids": ids}


# ---------------------------------------------------------------- hourly lane

def alerted_today(db, marketplace_day):
    keys = set()
    for job in _jobs(db, "hourly", "marketplace_day", marketplace_day):
        if job.status == "SUCCEEDED":
            keys.update(item for item in (job.metadata_json or {}).get("alerted", []) if isinstance(item, str))
    return keys


def in_hours(config, now):
    hour = now.astimezone(BEIJING).hour
    return config["start_hour"] <= hour < config["end_hour"]


def _labels(result, directory):
    operators = {key: directory_module.operator_label(directory, key) for key in result["profiles"]}
    return _names(result), operators


def run_hourly(db, settings, *, now, runner=None):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Lanes require a timezone-aware clock")
    config = lane_settings.load(settings)["lanes"]["hourly"]
    if not config["enabled"]:
        return {"status": "DISABLED"}
    if not in_hours(config, now):
        return {"status": "OUTSIDE_HOURS"}
    if blocked := _gate(settings, config, now):
        return {"status": blocked}
    slot = now.astimezone(BEIJING).strftime("%Y-%m-%dT%H")
    jobs = _jobs(db, "hourly", "slot", slot)
    if any(job.status in _TERMINAL_OK for job in jobs):
        return {"status": "ALREADY_COMPLETE"}
    if _retry_later(jobs, now, limit=3, pause=timedelta(minutes=15)):
        return {"status": "RETRY_LATER"}
    routing_data = recipients.routing(settings)
    plans = []
    if config["to_group"] and (group := recipients.group_delivery(settings, routing_data, "hourly")):
        plans.append(group)
    if config["to_lead"]:
        plans += recipients.lead_deliveries(settings, routing_data, "hourly")
    if not plans and not config["to_operators"]:
        return {"status": "NO_RECIPIENT"}
    directory = directory_module.load(settings)
    marketplace_day = tasks.marketplace_now(now).date().isoformat()
    try:
        result = _run(settings, "hourly", config, now=now, directory=directory, db=db, runner=runner,
                      alerted=frozenset(alerted_today(db, marketplace_day)))
    except (SparkXError, DeepSeekError, ValueError) as exc:
        failure = _failure(exc)
        _receipt(db, "hourly", "FAILED", now, slot=slot, **failure)
        return {"status": "FAILED", **failure}
    findings = result["findings"]
    names, operators = _labels(result, directory)
    extra = {"jx_slot": slot, "jx_marketplace_day": marketplace_day}
    ids, alerted, files = [], set(), []
    if findings:
        value = cards.hourly_card(findings, day=result["day"], time=result["time"], names=names,
                                  operators=operators)
        affected = {item["pid"] for item in findings}
        responsible = recipients.responsible(directory, affected, names)
        attachment = lead_value = None
        if value is not None:
            lead_value = cards.with_operator_line([value], cards.operator_line(*responsible))[0]
            attachment = save_book(settings, "hourly", lambda: alerts_book(result, findings, names, operators, now,
                                                                           responsible=responsible), now)
            files += [attachment["name"]] if attachment else []
        plans = _mention_operators(plans, settings, routing_data, directory, config, "hourly", affected, names)
        for delivery in plans:
            group_extra = {**extra, "jx_profiles": sorted(affected, key=int)}
            card = _for(delivery, "hourly", value, lead_value)
            if value is not None and (row_id := _enqueue(db, "hourly", slot, delivery, card, now, group_extra)):
                ids.append(row_id)
                ids += _enqueue_file(db, "hourly", slot, delivery, attachment, now, group_extra)
                alerted.update(tasks.finding_key(item) for item in findings)
        if config["to_operators"]:
            leads = {plan["receive_id"] for plan in plans if plan["receive_id_type"] == "open_id"}
            by_store = defaultdict(list)
            for item in findings:
                by_store[item["pid"]].append(item)
            for delivery in recipients.operator_deliveries(settings, routing_data, directory, "hourly",
                                                          set(by_store), exclude=leads):
                own = [item for key in delivery["profiles"] for item in by_store[key]]
                scoped = cards.hourly_card(own, day=result["day"], time=result["time"], names=names,
                                           operators=operators, scoped=True)
                own_extra = {**extra, "jx_profiles": delivery["profiles"]}
                if scoped is not None and (row_id := _enqueue(db, "hourly", slot, delivery, scoped, now, own_extra)):
                    ids.append(row_id)
                    copy = save_book(settings, "hourly", lambda: alerts_book(result, own, names, operators, now,
                                                                             scoped=True), now, who=delivery["name"])
                    files += [copy["name"]] if copy else []
                    ids += _enqueue_file(db, "hourly", slot, delivery, copy, now, own_extra)
                    alerted.update(tasks.finding_key(item) for item in own)
    db.flush()
    _receipt(db, "hourly", "SUCCEEDED", now, slot=slot, marketplace_day=marketplace_day, outbox_ids=ids,
             alerted=sorted(alerted), findings=len(findings), dropped=result.get("dropped", 0), files=files,
             store_count=len(result["profiles"]), **_summary(result))
    return {"status": "QUEUED" if ids else "NO_FINDINGS", "outbox_ids": ids}


# ---------------------------------------------------------------- console preview

def _audience(settings, directory, pids, names):
    """The stores a preview is about and the operators its group copy would @ right now."""
    stores = {key: names.get(key, key) for key in sorted(pids, key=int)}
    try:
        mentions = recipients.operator_mentions(settings, recipients.routing(settings), directory, pids)
    except (OSError, RuntimeError, ValueError):
        mentions = {}
    people = {row["open_id"]: row["name"] for key in pids for row in directory_module.operators(directory, key)}
    return {"stores": stores, "operators": [{"name": people.get(uid, "运营"), "stores": [stores[key] for key in keys]}
                                            for uid, keys in mentions.items()]}


def preview(settings, lane, *, now, db, runner=None, config=None):
    """Live and read-only: what the lane would freeze now for the group (nothing is queued).

    ``info["audience"]`` names the stores the cards are about and the operators a group
    copy would @, which is what a console test send of these cards uses.
    """
    if lane not in LANES:
        raise ValueError("Unknown lane")
    config = config or lane_settings.load(settings)["lanes"][lane]
    directory = directory_module.load(settings)
    alerted = frozenset()
    if lane == "hourly":
        alerted = frozenset(alerted_today(db, tasks.marketplace_now(now).date().isoformat()))
    result = _run(settings, lane, config, now=now, directory=directory, db=db, runner=runner, alerted=alerted)
    info = {"profiles": len(result["profiles"]), **_summary(result)}
    names = _names(result)
    if lane == "hourly":
        _, operators = _labels(result, directory)
        value = cards.hourly_card(result["findings"], day=result["day"], time=result["time"], names=names,
                                  operators=operators)
        affected = {item["pid"] for item in result["findings"]}
        book = alerts_book(result, result["findings"], names, operators, now,
                           responsible=recipients.responsible(directory, affected, names)) if value else None
        return ([value] if value else []), {**info, "day": result["day"], "findings": len(result["findings"]),
                                            "dropped": result.get("dropped", 0), "note": result.get("note"),
                                            "audience": _audience(settings, directory, affected, names),
                                            "workbook": book}
    color = "blue" if lane == "daily" else ("orange" if result["stores"] else "green")
    title = _title(lane, result)
    values = cards.report_cards(title, result["content"], color=color)
    responsible = recipients.responsible(directory, set(result["stores"]), names)
    return values, {**info, "report_date": result["report_date"], "window": result["window"],
                    "stores": len(result["stores"]),
                    "audience": _audience(settings, directory, set(result["stores"]), names),
                    "workbook": report_book(lane, result, result["content"], now, title=title,
                                            responsible=responsible)}


# ---------------------------------------------------------------- console test sends

def queue_test(db, settings, lane, values, *, now, preview_id, stores, to_group, to_lead, mention, book=None):
    """Queue the owner's test send of a finished preview: the same cards and its Excel copy, with no
    测试 wording (owner, Nov 2026) — only the keys, receipts and archive folder tell it apart.

    ``stores`` ({profile id: name}) are the stores the preview is about; with ``mention``
    the group copy @-mentions their current operators. ``to_lead`` reaches every lead of
    the lane. Test sends use their own keys and receipt type: they never count as the
    lane's run, never mark hourly alerts as sent and are not tied to the lane's schedule
    or switches; their Excel files are kept apart (data/reports/tests). Each recipient is
    re-checked before sending, and nothing goes out 30 minutes after queueing.
    """
    if lane not in LANES or not values or not (to_group or to_lead):
        raise ValueError("Nothing to send")
    try:
        routing_data = recipients.routing(settings)
    except (OSError, RuntimeError, ValueError):
        return {"status": "ROUTING_UNAVAILABLE"}
    group = recipients.group_delivery(settings, routing_data, lane) if to_group else None
    leads = recipients.lead_deliveries(settings, routing_data, lane) if to_lead else []
    missing = [name for name, wanted, found in (("group", to_group, group), ("lead", to_lead, leads))
               if wanted and not found]
    if missing:
        return {"status": "NO_RECIPIENT", "missing": missing}
    directory = directory_module.load(settings)
    mentions = {}
    if group is not None and mention and stores:
        mentions = recipients.operator_mentions(settings, routing_data, directory, set(stores))
        if mentions:
            group = recipients.with_operator_mentions(group, mentions, stores)
    marked = cards.test_copies(values)
    # Like the real lead copies, a lead's test copy names the operators of these stores.
    lead_marked = cards.with_operator_line(marked, cards.operator_line(*recipients.responsible(
        directory, set(stores), stores)))
    attachment = save_book(settings, "tests", book, now)
    extra = {"jx_test": True, "jx_test_mentions": bool(mentions), "jx_profiles": sorted(stores, key=int)}
    ids, cards_queued, files = [], 0, 0
    for delivery in ([group] if group else []) + leads:
        queued = _enqueue_cards(db, lane, preview_id, delivery, _for(delivery, lane, marked, lead_marked), now, extra,
                                scope=f"test:{lane}")
        if queued:
            cards_queued += len(queued)
            copy = _enqueue_file(db, lane, preview_id, delivery, attachment, now, extra, scope=f"test:{lane}")
            files += len(copy)
            ids += queued + copy
    if not ids:
        return {"status": "ALREADY_QUEUED", "outbox_ids": [], "mentions": 0, "cards": 0, "files": 0}
    from jx_alert.storage.models import SyncJob

    instant = _utc(now)
    db.add(SyncJob(job_type=TEST_JOB, status="SUCCEEDED", started_at=instant, finished_at=instant, store_count=0,
                   metadata_json={"lane": lane, "preview": preview_id, "outbox_ids": ids, "cards": len(marked),
                                  "targets": [name for name, found in (("group", group), ("lead", leads)) if found],
                                  "leads": len(leads), "mentions": len(mentions),
                                  "file": attachment["name"] if attachment else None}))
    db.flush()
    return {"status": "QUEUED", "outbox_ids": ids, "mentions": len(mentions), "cards": cards_queued, "files": files}
