"""Recipient-scoped, evidence-backed advertising batches and Beijing quiet hours."""
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_

from jx_alert.ai.ad_review_skills import load_ad_review_skills
from jx_alert.notifications.ad_repeat import (
    INCIDENT, campaign_identity, incident_key, incident_metadata, load_incident_history, repeat_eligible_signals,
)
from jx_alert.notifications.cards import card, number, plain, with_mentions
from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification
from jx_alert.notifications.recipient_policy import ROLE, advertising_subscriber_deliveries
from jx_alert.notifications.routing import (
    ApplicationRoutingError, _load_routing, report_group_delivery, report_group_owner_mentions,
)
from jx_alert.notifications.store_health import card_fits
from jx_alert.storage.models import Campaign, Store, SyncJob
from jx_alert.store_controls import notification_states


BEIJING = ZoneInfo('Asia/Shanghai')
OPENS, CLOSES = time(8), time(18)
TITLES = {
    'CTR': '点击率下降', 'CPC': '点击花费上升', 'CDR': '点击转化率下降',
    'CPS': '单件转化花费上升', 'CPA': '单均花费上升', 'ROAS': '广告投入产出下降',
    'spend': '花费上升', 'orders': '成熟归因订单下降',
    'zero_conversion': '成熟广告有花费但无归因订单', 'intraday_budget': '盘中花费超出复核线',
}
TRAFFIC = {'spend', 'CTR', 'CPC'}


def _routing(settings):
    try:
        return _load_routing(settings.feishu_routing_file)
    except (OSError, RuntimeError, ValueError, TypeError):
        return {}


def _group(settings, routing):
    try:
        return report_group_delivery(settings, routing, role='group')
    except ApplicationRoutingError:
        return None


def _metric(value, code, currency):
    if code in {'CTR', 'CDR'}:
        return f'{value:.2%}'
    if code == 'ROAS':
        return f'{value:.2f} 倍'
    return number(value, money=code != 'orders', currency=currency)


def alert_item(finding, *, include_dates=False):
    """Persist only the identifiers and numeric evidence shown in the alert."""
    row, signals = finding['evidence'], finding['signals']
    currency = row['currency']
    text = []
    if row.get('intraday'):
        text.append('花费 ' + number(row['current']['spend'], money=True, currency=currency))
        text.append('日预算 ' + number(row['verified_daily_budget'], money=True, currency=currency))
    else:
        current, mature = row['current'], row['mature']
        if TRAFFIC.intersection(signals):
            label = ('日报花费 ' + row['report_date']) if include_dates else '日报花费'
            text.append(label + ' ' + number(current['spend'], money=True, currency=currency))
        if set(signals) - TRAFFIC:
            label = ('成熟花费 ' + row['mature_date']) if include_dates else '成熟花费'
            text.append(label + ' ' + number(mature['spend'], money=True, currency=currency))
            text.append('点击 ' + number(mature['clicks']) + ' · 归因订单 ' + number(mature['orders']))
        for code in signals:
            metric = row['kpis'].get(code, {})
            a, b = metric.get('current'), metric.get('baseline')
            if a is not None and b is not None:
                text.append(f'{code} {_metric(a, code, currency)}，参考 {_metric(b, code, currency)}')
    return {field: row[field] for field in ('store_id', 'ad_type', 'campaign_id')} | {
        'store': plain(row['store_name'], 64), 'campaign': plain(row['campaign_name'], 105),
        'signal': '；'.join(TITLES[code] for code in signals), 'numbers': '\n'.join(text),
    }


def advertising_card(items, *, title='广告异常', store_mentions=None):
    def render(rows):
        checks = [(i['store'] + '\n' + i['campaign'], i['signal'], i['numbers']) for i in rows]
        notes = [f'另有 {len(items) - len(rows)} 项异常已保留记录。'] if len(rows) < len(items) else []
        value = card(title, subtitle='', sections=[('', ['店铺／活动', '异常', '关键数据'], checks)],
                     notes=notes, color='orange')
        value['body']['elements'].pop(0)
        if store_mentions:
            mentions = list(dict.fromkeys(uid for row in rows for uid in store_mentions.get(row['store_id'], ())))
            value = with_mentions(value, mentions)
        return value

    # Keep one recipient message and use native table pagination within it.
    visible = items[:50]
    result = render(visible)
    while len(visible) > 1 and not card_fits(result):
        visible = visible[:-1]
        result = render(visible)
    return result


def delivery_window(payload, now):
    """Absolute Beijing deadline, applied again just before provider delivery."""
    if not payload.get('hourly_ad_monitor'):
        return 'READY', None
    local = now.astimezone(BEIJING)
    if payload.get('hourly_delivery_date') is not None:
        if payload['hourly_delivery_date'] != local.date().isoformat() or local.time() >= CLOSES:
            return 'EXPIRED', None
    if OPENS <= local.time() < CLOSES:
        return 'READY', None
    day = local.date() + timedelta(days=local.time() >= CLOSES)
    due = datetime.combine(day, OPENS, BEIJING)
    return 'WAITING', due.astimezone(timezone.utc).replace(tzinfo=None)


def _key(bucket, destination):
    raw = json.dumps([bucket, destination['receive_id_type'], destination['receive_id']])
    return 'app:hourly-batch:' + hashlib.sha256(raw.encode()).hexdigest()


def _skill_fingerprint(profile):
    raw = json.dumps(profile, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _group_mentions(routing, delivery, items):
    return {sid: report_group_owner_mentions(routing, delivery, sid) for sid in {i['store_id'] for i in items}}


def _write_batch(db, bucket, destination, items, day, *, group, routing, overnight):
    key = _key(bucket, destination)
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)):
        return None
    mentions = _group_mentions(routing, group, items) if destination == group else None
    value = advertising_card(items, title='夜间广告异常汇总' if overnight else '广告异常', store_mentions=mentions)
    row = enqueue_notification(db, idempotency_key=key, text=value['header']['title']['content'],
        chat_id=destination['receive_id'], store_ids=sorted({i['store_id'] for i in items}),
        fixed_route=True, delivery=destination, card=value, material_ad_alert=True)
    row.payload = {**row.payload, 'hourly_ad_monitor': True, 'hourly_items': items,
                   'hourly_delivery_date': day.isoformat(), 'hourly_overnight': overnight,
                   'hourly_group_delivery': group}
    return row.id


def eligible_items(db, settings, items, now):
    ids = {campaign_identity(i) for i in items}
    if not ids:
        return []
    skills = load_ad_review_skills(settings)
    if skills.status == 'invalid':
        return []
    clock = now.astimezone(timezone.utc).replace(tzinfo=None)
    controls = notification_states(settings, {item[0] for item in ids})
    ready = set()
    for campaign in db.scalars(select(Campaign).join(Store, Store.sid == Campaign.store_id).where(
            Store.status == 1, Campaign.state == 'enabled',
            Campaign.updated_at.between(clock - timedelta(minutes=90), clock),
            tuple_(Campaign.store_id, Campaign.campaign_type, Campaign.campaign_id).in_(ids))):
        ready.add((campaign.store_id, campaign.campaign_type, campaign.campaign_id))
    return [i for i in items if campaign_identity(i) in ready and controls[i['store_id']][0]
            and i.get('skill_fingerprint') == _skill_fingerprint(skills.for_campaign(
                i['store_id'], i['ad_type'], i['campaign_id']))
            and (controls[i['store_id']][1] is None
                 or controls[i['store_id']][1] < datetime.fromisoformat(i['detected_at']))]


def _write_routes(db, settings, bucket, items, day, *, overnight=False):
    routing = _routing(settings)
    group = _group(settings, routing)
    if not group or not items:
        return []
    primary = _write_batch(db, bucket, group, items, day, group=group, routing=routing, overnight=overnight)
    if primary is None:
        # Keep late findings eligible for the next cycle rather than sending a
        # second group message or a private-only batch in this cycle.
        return []
    lead = advertising_subscriber_deliveries(settings)
    if len(lead) != 1:
        return [primary]
    copy = _write_batch(db, bucket, lead[0], items, day, group=group, routing=routing, overnight=overnight)
    return [primary, copy] if copy is not None else [primary]


def queue_hourly_findings(db, settings, findings, *, now, slot, send_alerts):
    if not settings.feishu_enabled or settings.feishu_transport != 'app' or not send_alerts:
        return []
    local = now.astimezone(BEIJING)
    day = local.date()
    overnight = not OPENS <= local.time() < CLOSES
    history = load_incident_history(db, now)
    skills = load_ad_review_skills(settings)
    candidates = {}
    for finding in findings:
        evidence = finding['evidence']
        identity = campaign_identity(evidence)
        if identity in candidates:
            continue
        signals = repeat_eligible_signals(evidence, finding['signals'], history, now)
        if not signals:
            continue
        key = incident_key(evidence)
        exists = db.scalar(select(SyncJob.id).where(SyncJob.job_type == INCIDENT,
            SyncJob.metadata_json['incident_key'].as_string() == key))
        if not exists:
            profile = evidence.get('ad_skill') or skills.for_campaign(*identity)
            item = alert_item({**finding, 'signals': signals}) | {
                'detected_at': now.isoformat(), 'skill_fingerprint': _skill_fingerprint(profile),
            }
            candidates[identity] = incident_metadata(evidence, signals), item
    valid = eligible_items(db, settings, [i for _, i in candidates.values()], now)
    ids = [] if overnight else _write_routes(db, settings, slot, valid, day)
    if not overnight and not ids:
        return []
    for item in valid:
        identity = campaign_identity(item)
        meta = {**candidates[identity][0], 'outbox_ids': ids}
        if overnight:
            release = day + timedelta(days=local.time() >= CLOSES)
            meta.update(release_day=release.isoformat(), hourly_item=item)
        db.add(SyncJob(job_type=INCIDENT, status='HELD' if overnight else 'QUEUED',
            started_at=now.astimezone(timezone.utc).replace(tzinfo=None), metadata_json=meta))
    db.commit()
    return ids


def queue_overnight_digest(db, settings, now):
    local = now.astimezone(BEIJING)
    if not settings.feishu_enabled or settings.feishu_transport != 'app' or not OPENS <= local.time() < CLOSES:
        return []
    day = local.date()
    bucket = 'overnight:' + day.isoformat()
    if db.scalar(select(SyncJob.id).where(SyncJob.job_type == 'overnight_ad_digest',
            SyncJob.started_at >= datetime.combine(day, OPENS, BEIJING).astimezone(timezone.utc)
            .replace(tzinfo=None)).limit(1)):
        return []
    held = list(db.scalars(select(SyncJob).where(SyncJob.job_type == INCIDENT, SyncJob.status == 'HELD',
        SyncJob.metadata_json['release_day'].as_string() == day.isoformat()).order_by(SyncJob.id)))
    items = eligible_items(db, settings, [job.metadata_json['hourly_item'] for job in held], now)
    ids = _write_routes(db, settings, bucket, items, day, overnight=True)
    if not ids:
        return []
    included = {campaign_identity(i) for i in items}
    for job in held:
        if campaign_identity(job.metadata_json['hourly_item']) in included:
            job.status = 'QUEUED'
            job.metadata_json = {**job.metadata_json, 'outbox_ids': ids}
    db.add(SyncJob(job_type='overnight_ad_digest', status='QUEUED',
        started_at=now.astimezone(timezone.utc).replace(tzinfo=None),
        metadata_json={'release_day': day.isoformat(), 'outbox_ids': ids}))
    db.commit()
    return ids


def recheck_hourly_card(db, settings, payload, now):
    """Keep only authorized, still-enabled activities in this exact destination."""
    destination = payload['delivery']
    routing = _routing(settings)
    current = _group(settings, routing)
    if current is None:
        return None
    if destination.get('role') == ROLE:
        if (payload.get('hourly_group_delivery') != current
                or advertising_subscriber_deliveries(settings) != [destination]):
            return None
    elif current != destination:
        return None
    rows = eligible_items(db, settings, payload['hourly_items'], now)
    if not rows:
        return None
    mentions = _group_mentions(routing, current, rows) if destination == current else None
    return advertising_card(rows, title='夜间广告异常汇总' if payload['hourly_overnight'] else '广告异常',
                            store_mentions=mentions)
