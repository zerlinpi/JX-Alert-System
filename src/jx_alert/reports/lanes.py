"""Queue the SparkX lanes into the durable outbox and gate every later send.

daily   08:30 Beijing: company KPIs, site and store tables, 须知 → report group + 总负责人
review  08:30 Beijing: 7-day campaign anomaly review → report group (@ the affected stores'
        operators once mapped) + 广告负责人; optional private copies per operator
hourly  every hour inside the owner's window: wasteful/opportunity campaigns today
        → report group (@ operators) + 广告负责人; each campaign/category once per day

All lanes are on by default and still notify the group when no operator is mapped.

One SparkX conversation per run; cards are frozen at queue time. Before each
POST the worker re-checks the switch, the window and the exact recipient.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select

from jx_alert.reports import cards, recipients
from jx_alert.reports import settings as lane_settings
from jx_alert.sparkx import analysis
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


def _key(lane, slot, delivery):
    digest = hashlib.sha256(json.dumps([delivery["role"], delivery["receive_id"]]).encode()).hexdigest()[:24]
    return f"{PREFIX}{lane}:{slot}:{digest}"


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


def _sparkx_ready(settings):
    from jx_alert.sparkx.credentials import effective_token

    return bool(effective_token(settings))


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


def _people(directory, profiles):
    return {key: directory_module.operator_label(directory, key) for key in profiles}


def _enqueue(db, lane, slot, delivery, card, now, extra):
    from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification

    key = _key(lane, slot, delivery)
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)) is not None:
        return None  # Never rewrite a frozen card for the same slot and recipient.
    row = enqueue_notification(db, idempotency_key=key, text=card["header"]["title"]["content"],
                               delivery=delivery, card=card, extra={
                                   "jx_lane": lane, "jx_created_at": now.astimezone(timezone.utc).isoformat(),
                                   **extra})
    return row.id


def _collect(settings, lane, now, db, directory, collector):
    from jx_alert.sparkx.collect import collect

    def remember(roster):
        try:
            with db.begin_nested():
                directory_module.record_roster(db, roster, now)
        except Exception:  # noqa: BLE001 - a snapshot must never fail the lane.
            pass

    return (collector or collect)(settings, lane, now=now, directory=directory, on_roster=remember)


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


def queue_morning_lane(db, settings, lane, *, now, collector=None, narrator=None):
    """Collect once, freeze cards and enqueue one message per recipient for today's Beijing date."""
    from jx_alert.ai import narrative

    if lane not in {"daily", "review"}:
        raise ValueError("Unknown morning lane")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Lanes require a timezone-aware clock")
    config = lane_settings.load(settings)["lanes"][lane]
    if not config["enabled"]:
        return {"status": "DISABLED"}
    if not settings.delivery_enabled():
        return {"status": "DELIVERY_DISABLED"}
    local = now.astimezone(BEIJING)
    if not OPENS <= local.time() < GENERATE_UNTIL:
        return {"status": "WAITING"}
    if lane == "review" and config["weekly"] and local.weekday() != 0:
        return {"status": "NOT_SCHEDULED_TODAY"}
    if not _sparkx_ready(settings):
        return {"status": "SPARKX_NOT_CONFIGURED"}  # No receipt: nothing to retry until a token is saved.
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
        collected = _collect(settings, lane, now, db, directory, collector)
    except SparkXError as exc:
        _receipt(db, lane, "FAILED", now, day=day, error=exc.code)
        return {"status": "FAILED", "error": exc.code}
    people = _people(directory, collected["profiles"])
    extra = {"jx_day": day}
    ids, skipped = [], 0
    if lane == "daily":
        report = analysis.build_daily(collected, people)
        if config["ai"]:
            notes, source = (narrator or narrative.daily_notes)(settings, report)
        else:
            notes, source = [{"title": "", "text": text} for text in report["notes"]], "rules"
        value = cards.daily_card(report, notes, source)
        extra.update(jx_report_date=report["report_date"], jx_profiles=sorted(collected["profiles"], key=int))
        for delivery in plans:
            if value is None:
                skipped += 1
                continue
            row_id = _enqueue(db, lane, day, delivery, value, now, extra)
            ids += [row_id] if row_id else []
        summary = {"report_date": report["report_date"], "narrative": source,
                   "with_spend": report["company"]["with_spend"]}
    else:
        report = analysis.build_review(collected, people)
        if config["ai"]:
            text, source = (narrator or narrative.review_text)(settings, report)
        else:
            text, source = {"headline": None, "actions": report["actions"]}, "rules"
        value = cards.review_card(report, text, source)
        extra.update(jx_window=report["window"], jx_profiles=sorted(collected["profiles"], key=int))
        affected = {row["pid"] for row in report["findings"] + report["zero_stores"]}
        plans = _mention_operators(plans, settings, routing_data, directory, config, lane, affected)
        for delivery in plans:
            if value is None:
                skipped += 1
                continue
            row_id = _enqueue(db, lane, day, delivery, value, now, extra)
            ids += [row_id] if row_id else []
        if config["to_operators"]:
            leads = {plan["receive_id"] for plan in plans if plan["receive_id_type"] == "open_id"}
            for delivery in recipients.operator_deliveries(settings, routing_data, directory, lane, affected,
                                                          exclude=leads):
                scoped = analysis.review_slice(report, set(delivery["profiles"]))
                names = {row["name"] for row in scoped["overview"] + scoped["zero_stores"]}
                scoped_text = {"headline": None,
                               "actions": [row for row in text["actions"] if row.get("store") in names]}
                scoped_value = cards.review_card(scoped, scoped_text, source)
                if scoped_value is None:
                    skipped += 1
                    continue
                row_id = _enqueue(db, lane, day, delivery, scoped_value, now,
                                  {**extra, "jx_profiles": delivery["profiles"]})
                ids += [row_id] if row_id else []
        summary = {"window": report["window"], "narrative": source, "findings": len(report["findings"]),
                   "zero_stores": len(report["zero_stores"])}
    db.flush()
    _receipt(db, lane, "SUCCEEDED", now, day=day, outbox_ids=ids, skipped=skipped,
             store_count=len(collected["profiles"]), errors=collected["errors"], **summary)
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


def run_hourly(db, settings, *, now, collector=None):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Lanes require a timezone-aware clock")
    config = lane_settings.load(settings)["lanes"]["hourly"]
    if not config["enabled"]:
        return {"status": "DISABLED"}
    if not settings.delivery_enabled():
        return {"status": "DELIVERY_DISABLED"}
    if not in_hours(config, now):
        return {"status": "OUTSIDE_HOURS"}
    if not _sparkx_ready(settings):
        return {"status": "SPARKX_NOT_CONFIGURED"}
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
    try:
        collected = _collect(settings, "hourly", now, db, directory, collector)
    except SparkXError as exc:
        _receipt(db, "hourly", "FAILED", now, slot=slot, error=exc.code)
        return {"status": "FAILED", "error": exc.code}
    marketplace_day = collected["day"]
    thresholds = {key: config[key] for key in analysis.HOURLY_DEFAULTS}
    report = analysis.build_hourly(collected, _people(directory, collected["profiles"]), thresholds,
                                   alerted=alerted_today(db, marketplace_day))
    extra = {"jx_slot": slot, "jx_marketplace_day": marketplace_day}
    ids, alerted = [], set()
    if report["new"]:
        value = cards.hourly_card(report)
        plans = _mention_operators(plans, settings, routing_data, directory, config, "hourly",
                                   {item["pid"] for item in report["new"]})
        for delivery in plans:
            if value is not None and (row_id := _enqueue(db, "hourly", slot, delivery, value, now, {
                    **extra, "jx_profiles": sorted({item["pid"] for item in report["new"]}, key=int)})):
                ids.append(row_id)
                alerted.update(analysis.finding_key(item) for item in report["new"])
        if config["to_operators"]:
            leads = {plan["receive_id"] for plan in plans if plan["receive_id_type"] == "open_id"}
            by_store = defaultdict(list)
            for item in report["new"]:
                by_store[item["pid"]].append(item)
            for delivery in recipients.operator_deliveries(settings, routing_data, directory, "hourly",
                                                          set(by_store), exclude=leads):
                scoped = analysis.hourly_slice(report, set(delivery["profiles"]))
                value = cards.hourly_card(scoped)
                if value is not None and (row_id := _enqueue(db, "hourly", slot, delivery, value, now,
                                                              {**extra, "jx_profiles": delivery["profiles"]})):
                    ids.append(row_id)
                    alerted.update(analysis.finding_key(item) for item in scoped["new"])
    db.flush()
    _receipt(db, "hourly", "SUCCEEDED", now, slot=slot, marketplace_day=marketplace_day, outbox_ids=ids,
             alerted=sorted(alerted), findings=len(report["findings"]), new=len(report["new"]),
             store_count=len(collected["profiles"]), errors=collected["errors"])
    return {"status": "QUEUED" if ids else "NO_FINDINGS", "outbox_ids": ids}


# ---------------------------------------------------------------- console preview

def preview(settings, lane, *, now, db, collector=None, narrator=None):
    """Live and read-only: the card the lane would freeze now for the lead/group view."""
    from jx_alert.ai import narrative

    if lane not in LANES:
        raise ValueError("Unknown lane")
    config = lane_settings.load(settings)["lanes"][lane]
    directory = directory_module.load(settings)
    collected = _collect(settings, lane, now, db, directory, collector)
    people = _people(directory, collected["profiles"])
    info = {"profiles": len(collected["profiles"]), "errors": collected["errors"],
            "disabled": len(collected["disabled"]), "missing": len(collected["missing"])}
    if lane == "daily":
        report = analysis.build_daily(collected, people)
        notes, source = ((narrator or narrative.daily_notes)(settings, report) if config["ai"]
                         else ([{"title": "", "text": text} for text in report["notes"]], "rules"))
        return cards.daily_card(report, notes, source), {**info, "report_date": report["report_date"],
                                                          "narrative": source}
    if lane == "review":
        report = analysis.build_review(collected, people)
        text, source = ((narrator or narrative.review_text)(settings, report) if config["ai"]
                        else ({"headline": None, "actions": report["actions"]}, "rules"))
        return cards.review_card(report, text, source), {**info, "window": report["window"], "narrative": source,
                                                          "findings": len(report["findings"])}
    thresholds = {key: config[key] for key in analysis.HOURLY_DEFAULTS}
    report = analysis.build_hourly(collected, people, thresholds)
    return cards.hourly_card(report), {**info, "day": report["day"], "findings": len(report["findings"])}
