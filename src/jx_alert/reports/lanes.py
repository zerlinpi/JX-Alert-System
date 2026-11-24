"""Queue the lanes into the durable outbox and gate every later send.

daily   08:30 Beijing: DeepSeek queries SparkX and writes the daily report → report group + 总负责人
review  08:30 Beijing: DeepSeek's 7-day campaign anomaly review → report group (@ the affected stores'
        operators once mapped) + 广告负责人; optional private copies per operator
hourly  every hour inside the owner's window: wasteful/opportunity campaigns today, found by
        DeepSeek and checked against the SparkX rows → report group (@ operators) + 广告负责人;
        each campaign/category once per marketplace day

All lanes are on by default and still notify the group when no operator is mapped.
Cards are frozen at queue time; before each POST the worker re-checks the switch,
the window and the exact recipient.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select

from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.reports import cards, recipients, tasks
from jx_alert.reports import settings as lane_settings
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import SparkXError

BEIJING = ZoneInfo("Asia/Shanghai")
OPENS, GENERATE_UNTIL, SEND_UNTIL = time(8, 30), time(10, 0), time(12, 0)
HOURLY_TTL = timedelta(minutes=50)
PREFIX = "jx:"
LANES = ("daily", "review", "hourly")
JOBS = {lane: f"jx_{lane}" for lane in LANES}
_TERMINAL_OK = {"SUCCEEDED"}


def _utc(now):
    return now.astimezone(timezone.utc).replace(tzinfo=None)


def is_lane(key, payload):
    return isinstance(key, str) and key.startswith(PREFIX) and payload.get("jx_lane") in LANES


def _key(lane, slot, delivery, part=0):
    digest = hashlib.sha256(json.dumps([delivery["role"], delivery["receive_id"]]).encode()).hexdigest()[:24]
    return f"{PREFIX}{lane}:{slot}:{digest}" + (f":{part}" if part else "")


def delivery_window(key, payload, now):
    """READY / WAITING (with due UTC) / EXPIRED, from the frozen payload only."""
    instant = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    lane = payload.get("jx_lane")
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


def send_mentions(settings, payload):
    """@-mentions for this send: frozen operators that are still mapped, still in the group,
    and only while the owner keeps 「在群内 @ 对应运营」 on. Private messages mention no one."""
    delivery = payload.get("delivery") or {}
    lane = payload.get("jx_lane")
    if delivery.get("role") != recipients.role(lane, "group") or not delivery.get("open_ids"):
        return []
    try:
        if not lane_settings.load(settings)["lanes"][lane].get("mention_operators"):
            return []
    except (lane_settings.SettingsError, KeyError):
        return []
    return recipients.current_mentions(settings, delivery)


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


def _enqueue(db, lane, slot, delivery, card, now, extra, part=0):
    from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification

    key = _key(lane, slot, delivery, part)
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)) is not None:
        return None  # Never rewrite a frozen card for the same slot and recipient.
    row = enqueue_notification(db, idempotency_key=key, text=card["header"]["title"]["content"],
                               delivery=delivery, card=card, extra={
                                   "jx_lane": lane, "jx_created_at": now.astimezone(timezone.utc).isoformat(),
                                   **extra})
    return row.id


def _enqueue_cards(db, lane, slot, delivery, values, now, extra):
    ids = []
    for part, value in enumerate(values):
        # Only the first card of a group message carries the @-mentions.
        target = delivery if part == 0 else {**delivery, "open_ids": [], "mentions": {}}
        row_id = _enqueue(db, lane, slot, target, value, now, extra, part)
        ids += [row_id] if row_id else []
    return ids


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


def _footer(result):
    summary = result["summary"]
    model = summary.get("model") or "DeepSeek"
    skills = f"，按 SparkX 官方 Skills {result['skills']}" if result.get("skills") else ""
    return (f"由 DeepSeek（{model}）调用 SparkX MCP 只读查询 {summary.get('sparkx_calls', 0)} 次生成{skills}。"
            "只读分析，不会修改任何广告、预算或出价。")


def _summary(result):
    summary = result["summary"]
    return {"sparkx_calls": summary.get("sparkx_calls"), "skill_reads": summary.get("skill_reads"),
            "rounds": summary.get("rounds"), "model": summary.get("model"), "usage": summary.get("usage"),
            "skills": result.get("skills")}


def _title(lane, result):
    if lane == "daily":
        return f"SparkX 广告日报 · {result['report_date']}"
    start, end = result["window"]
    return f"SparkX 广告异常复盘 · {start[5:]}~{end[5:]} 对比前 7 日"


# ---------------------------------------------------------------- 08:30 lanes

def _morning_plan(settings, routing_data, lane, config):
    plans = []
    if config["to_group"] and (group := recipients.group_delivery(settings, routing_data, lane)):
        plans.append(group)
    if config["to_lead"] and (lead := recipients.lead_delivery(settings, routing_data, lane)):
        plans.append(lead)
    return plans


def _mention_operators(plans, settings, routing_data, directory, config, lane, pids):
    """The group copy @-mentions the mapped operators of these stores; nobody else changes."""
    if not config.get("mention_operators"):
        return plans
    mentions = recipients.operator_mentions(settings, routing_data, directory, pids)
    group = recipients.role(lane, "group")
    return [recipients.with_operator_mentions(plan, mentions) if plan["role"] == group and mentions else plan
            for plan in plans]


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
    footer = _footer(result)
    color = "blue" if lane == "daily" else ("orange" if result["stores"] else "green")
    values = cards.report_cards(_title(lane, result), result["content"], footer, color=color)
    extra = {"jx_day": day, "jx_profiles": result["profiles"]}
    if lane == "review":
        plans = _mention_operators(plans, settings, routing_data, directory, config, lane, set(result["stores"]))
    ids = []
    for delivery in plans:
        ids += _enqueue_cards(db, lane, day, delivery, values, now, extra)
    scoped = 0
    if lane == "review" and config["to_operators"] and result["stores"]:
        leads = {plan["receive_id"] for plan in plans if plan["receive_id_type"] == "open_id"}
        names = directory_module.display_names(result["roster"]) if result["roster"] else {}
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
            ids += _enqueue_cards(db, lane, day, delivery, cards.report_cards(
                _title(lane, result) + " · 你负责的店铺", text, footer, color=color), now,
                {**extra, "jx_profiles": delivery["profiles"]})
    db.flush()
    _receipt(db, lane, "SUCCEEDED", now, day=day, outbox_ids=ids, cards=len(values), stores=result["stores"],
             report_date=result["report_date"], window=result["window"], operator_copies=scoped,
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
    roster = result.get("roster") or {}
    names = directory_module.display_names(roster) if roster else {}
    operators = {key: directory_module.operator_label(directory, key) for key in result["profiles"]}
    return names, operators


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
    if config["to_lead"] and (lead := recipients.lead_delivery(settings, routing_data, "hourly")):
        plans.append(lead)
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
    footer = _footer(result)
    extra = {"jx_slot": slot, "jx_marketplace_day": marketplace_day}
    ids, alerted = [], set()
    if findings:
        value = cards.hourly_card(findings, day=result["day"], time=result["time"], names=names,
                                  operators=operators, config=config, footer=footer)
        affected = {item["pid"] for item in findings}
        plans = _mention_operators(plans, settings, routing_data, directory, config, "hourly", affected)
        for delivery in plans:
            if value is not None and (row_id := _enqueue(db, "hourly", slot, delivery, value, now, {
                    **extra, "jx_profiles": sorted(affected, key=int)})):
                ids.append(row_id)
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
                                           operators=operators, config=config, footer=footer, scoped=True)
                if scoped is not None and (row_id := _enqueue(db, "hourly", slot, delivery, scoped, now,
                                                              {**extra, "jx_profiles": delivery["profiles"]})):
                    ids.append(row_id)
                    alerted.update(tasks.finding_key(item) for item in own)
    db.flush()
    _receipt(db, "hourly", "SUCCEEDED", now, slot=slot, marketplace_day=marketplace_day, outbox_ids=ids,
             alerted=sorted(alerted), findings=len(findings), dropped=result.get("dropped", 0),
             store_count=len(result["profiles"]), **_summary(result))
    return {"status": "QUEUED" if ids else "NO_FINDINGS", "outbox_ids": ids}


# ---------------------------------------------------------------- console preview

def preview(settings, lane, *, now, db, runner=None, config=None):
    """Live and read-only: what the lane would freeze now for the group (nothing is queued)."""
    if lane not in LANES:
        raise ValueError("Unknown lane")
    config = config or lane_settings.load(settings)["lanes"][lane]
    directory = directory_module.load(settings)
    alerted = frozenset()
    if lane == "hourly":
        alerted = frozenset(alerted_today(db, tasks.marketplace_now(now).date().isoformat()))
    result = _run(settings, lane, config, now=now, directory=directory, db=db, runner=runner, alerted=alerted)
    info = {"profiles": len(result["profiles"]), **_summary(result)}
    if lane == "hourly":
        names, operators = _labels(result, directory)
        value = cards.hourly_card(result["findings"], day=result["day"], time=result["time"], names=names,
                                  operators=operators, config=config, footer=_footer(result))
        return ([value] if value else []), {**info, "day": result["day"], "findings": len(result["findings"]),
                                            "dropped": result.get("dropped", 0), "note": result.get("note")}
    color = "blue" if lane == "daily" else ("orange" if result["stores"] else "green")
    values = cards.report_cards(_title(lane, result), result["content"], _footer(result), color=color)
    return values, {**info, "report_date": result["report_date"], "window": result["window"],
                    "stores": len(result["stores"])}
