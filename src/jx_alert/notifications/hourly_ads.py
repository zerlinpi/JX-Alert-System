"""Recipient-scoped, evidence-backed advertising batches and Beijing quiet hours."""
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_

from jx_alert.notifications.cards import card, number, plain
from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification
from jx_alert.notifications.recipient_policy import advertising_subscriber_deliveries
from jx_alert.notifications.routing import (
    ApplicationRoutingError, _app_open_ids, _load_routing, _route_entry,
    group_member_mentions, report_group_delivery,
)
from jx_alert.notifications.store_health import card_fits
from jx_alert.storage.models import Campaign, Store, SyncJob
from jx_alert.store_controls import notification_states


BEIJING = ZoneInfo('Asia/Shanghai')
OPENS, CLOSES = time(8), time(18)
INCIDENT = 'hourly_ad_incident'
RECIPIENT = 'store_ad_recipient'
TITLES = {
    'CTR': '点击率下降', 'CPC': '点击花费上升', 'CDR': '点击转化率下降',
    'CPS': '单件转化花费上升', 'CPA': '单均花费上升', 'ROAS': '广告投入产出下降',
    'spend': '花费上升', 'orders': '成熟归因订单下降',
    'zero_conversion': '成熟广告有花费但无归因订单', 'intraday_budget': '盘中花费超预算',
}
TRAFFIC = {'spend', 'CTR', 'CPC'}


def campaign_identity(row):
    return row['store_id'], row['ad_type'], row['campaign_id']


def incident_key(row):
    identity = [row['store_id'], row['campaign_id'], row['ad_type'], row['report_date'],
                'intraday' if row.get('intraday') else 'daily_review']
    return 'hourly-ad:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def recent_incidents(db, now):
    threshold = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    rows = db.scalars(select(SyncJob.metadata_json).where(SyncJob.job_type == INCIDENT,
        SyncJob.started_at > threshold))
    return {(item.get('store_id'), item.get('ad_type', 'sp'), item.get('campaign_id'))
            for item in rows if isinstance(item, dict)}


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


def _owners(routing, sid):
    try:
        entry = _route_entry(routing.get('stores', {}), sid)
        alias = entry.get('chat_id') or routing.get('default_chat_id')
        group = routing.get('groups', {}).get(alias, {})
        if not isinstance(group.get('chat_id'), str) or not group['chat_id'].startswith('oc_'):
            return set()
        return set(group_member_mentions(group, _app_open_ids(entry.get('open_ids', []))))
    except (ApplicationRoutingError, TypeError, AttributeError):
        return set()


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


def advertising_card(items, *, title='广告异常'):
    def render(rows):
        checks = [(i['store'] + '\n' + i['campaign'], i['signal'], i['numbers']) for i in rows]
        notes = [f'另有 {len(items) - len(rows)} 项异常已保留记录。'] if len(rows) < len(items) else []
        value = card(title, subtitle='', sections=[('', ['店铺／活动', '异常', '关键数据'], checks)],
                     notes=notes, color='orange')
        value['body']['elements'].pop(0)
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


def _write_batch(db, bucket, destination, items, day, *, overnight=False):
    key = _key(bucket, destination)
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)):
        return None
    value = advertising_card(items, title='夜间广告异常汇总' if overnight else '广告异常')
    row = enqueue_notification(db, idempotency_key=key, text=value['header']['title']['content'],
        chat_id=destination['receive_id'], store_ids=sorted({i['store_id'] for i in items}),
        fixed_route=True, delivery=destination, card=value, material_ad_alert=True)
    row.payload = {**row.payload, 'hourly_ad_monitor': True, 'hourly_items': items,
                   'hourly_delivery_date': day.isoformat(), 'hourly_overnight': overnight}
    return row.id


def eligible_items(db, settings, items, now):
    ids = {campaign_identity(i) for i in items}
    if not ids:
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
            and (controls[i['store_id']][1] is None
                 or controls[i['store_id']][1] < datetime.fromisoformat(i['detected_at']))]


def _route_items(db, settings, routing, items, slot):
    subscriber_routes = {r['receive_id']: r['subscription_generation'] for r in advertising_subscriber_deliveries(settings)}
    group = _group(settings, routing) if settings.feishu_group_fallback_enabled else None
    owners_by_store = {sid: _owners(routing, sid) for sid in {i['store_id'] for i in items}}
    routes = {
        uid: {'role': RECIPIENT, 'receive_id_type': 'open_id', 'receive_id': uid, 'open_ids': []}
        for uid in set(subscriber_routes).union(*owners_by_store.values())
    }
    taken = set(db.scalars(select(NotificationOutbox.idempotency_key).where(
        NotificationOutbox.idempotency_key.in_([_key(slot, r) for r in routes.values()]
                                              + ([_key(slot, group)] if group else [])))))
    by_person, fallback = defaultdict(list), []
    for item in items:
        owners = owners_by_store[item['store_id']]
        primary = [routes[uid] for uid in owners] if owners else ([group] if group else [])
        # Keep a late finding eligible for the next cycle instead of reporting
        # it only to subscribers when the intended owner's batch already closed.
        if any(_key(slot, route) in taken for route in primary):
            continue
        if not owners and group:
            fallback.append(item)
        for uid in owners | subscriber_routes.keys():
            by_person[uid].append({**item, 'owned': uid in owners, 'subscription': subscriber_routes.get(uid)})
    if fallback:
        yield group, fallback
    for uid, values in sorted(by_person.items()):
        if _key(slot, routes[uid]) not in taken:
            yield routes[uid], values


def queue_hourly_findings(db, settings, findings, *, now, slot, send_alerts):
    if not settings.feishu_enabled or settings.feishu_transport != 'app' or not send_alerts:
        return []
    local = now.astimezone(BEIJING)
    day = local.date()
    overnight = not OPENS <= local.time() < CLOSES
    routing = _routing(settings)
    recent = recent_incidents(db, now)
    candidates = {}
    for finding in findings:
        evidence = finding['evidence']
        identity = campaign_identity(evidence)
        if identity in recent or identity in candidates:
            continue
        key = incident_key(evidence)
        exists = db.scalar(select(SyncJob.id).where(SyncJob.job_type == INCIDENT,
            SyncJob.metadata_json['incident_key'].as_string() == key))
        if not exists:
            candidates[identity] = key, alert_item(finding) | {'detected_at': now.isoformat()}
    valid = eligible_items(db, settings, [i for _, i in candidates.values()], now)
    ids, queued = [], defaultdict(list)
    if not overnight:
        for dest, items in _route_items(db, settings, routing, valid, slot):
            oid = _write_batch(db, slot, dest, items, day)
            if oid is not None:
                ids.append(oid)
                for item in items:
                    queued[campaign_identity(item)].append(oid)
    for item in valid:
        identity = campaign_identity(item)
        if not overnight and identity not in queued:
            continue
        meta = {'incident_key': candidates[identity][0], 'store_id': item['store_id'],
                'ad_type': item['ad_type'], 'campaign_id': item['campaign_id'], 'outbox_ids': queued[identity]}
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
    destination = _group(settings, _routing(settings))
    if destination is None:
        return []
    bucket = 'overnight:' + day.isoformat()
    if db.scalar(select(SyncJob.id).where(SyncJob.job_type == 'overnight_ad_digest',
            SyncJob.started_at >= datetime.combine(day, OPENS, BEIJING).astimezone(timezone.utc)
            .replace(tzinfo=None)).limit(1)):
        return []
    held = list(db.scalars(select(SyncJob).where(SyncJob.job_type == INCIDENT, SyncJob.status == 'HELD',
        SyncJob.metadata_json['release_day'].as_string() == day.isoformat()).order_by(SyncJob.id)))
    items = eligible_items(db, settings, [job.metadata_json['hourly_item'] for job in held], now)
    if not items:
        return []
    oid = _write_batch(db, bucket, destination, items, day, overnight=True)
    if oid is None:
        return []
    included = {campaign_identity(i) for i in items}
    for job in held:
        if campaign_identity(job.metadata_json['hourly_item']) in included:
            job.status = 'QUEUED'
            job.metadata_json = {**job.metadata_json, 'outbox_ids': [oid]}
    db.add(SyncJob(job_type='overnight_ad_digest', status='QUEUED',
        started_at=now.astimezone(timezone.utc).replace(tzinfo=None),
        metadata_json={'release_day': day.isoformat(), 'outbox_ids': [oid]}))
    db.commit()
    return [oid]


def recheck_hourly_card(db, settings, payload, now):
    """Keep only authorized, still-enabled activities in this exact destination."""
    destination = payload['delivery']
    uid = destination['receive_id']
    routing = _routing(settings)
    rows = eligible_items(db, settings, payload['hourly_items'], now)
    if destination['role'] == RECIPIENT:
        subscribers = {r['receive_id']: r['subscription_generation'] for r in advertising_subscriber_deliveries(settings)}
        rows = [i for i in rows if (i.get('owned') and uid in _owners(routing, i['store_id']))
                or (i.get('subscription') and i['subscription'] == subscribers.get(uid))]
    else:
        current = _group(settings, routing)
        if current is None or current != destination:
            return None
        if not payload['hourly_overnight']:
            rows = [i for i in rows if settings.feishu_group_fallback_enabled and not _owners(routing, i['store_id'])]
    if not rows:
        return None
    return advertising_card(rows, title='夜间广告异常汇总' if payload['hourly_overnight'] else '广告异常')
