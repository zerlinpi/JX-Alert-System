"""Recipient-scoped, evidence-backed advertising batches and Beijing quiet hours."""
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, tuple_

from jx_alert.ai.ad_review_evidence import ctr_expected_click_gap
from jx_alert.ai.ad_review_skills import load_ad_review_skills, skill_fingerprint
from jx_alert.notifications.ad_repeat import (
    INCIDENT, campaign_identity, incident_already_recorded, incident_metadata,
    load_incident_history, repeat_eligible_signals, subscriber_reservations,
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
    'CTR': '看到广告的人更少点击', 'CPC': '每次点击更贵了', 'CDR': '点击后下单的比例下降',
    'CPS': '每卖出一件的广告费上涨', 'CPA': '每带来一单的广告费上涨', 'ROAS': '同样的广告费带来的销售额变少',
    'spend': '广告花费增加', 'orders': '广告带来的订单减少',
    'zero_conversion': '广告花了钱，统计期结束仍没带来订单', 'intraday_budget': '今天的花费远超平均日预算',
    'intraday_no_clicks': '今天有大量曝光，点击仍为零',
}
TRAFFIC = {'spend', 'CTR', 'CPC'}
_RATE_LABELS = {'CTR': '点击率', 'CDR': '点击转化率'}
_NEXT_CHECK = {
    'intraday_budget': '当天预算变更与自动预算规则',
    'intraday_no_clicks': '广告位表现与点击报表',
    'CTR': '搜索词与广告位',
    'CPC': '广告位花费和出价记录',
    'zero_conversion': '花费最多的投放与商品页',
    'CDR': '引流词与商品页',
    'CPS': '花费最多的投放与出单成本',
    'CPA': '花费最多的投放与出单成本',
    'ROAS': '归因销售额与花费最多的投放',
    'orders': '点击量与商品页转化',
}


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
    intraday = row.get('intraday') is True
    source = row.get('source')
    observed_at = row.get('observation_captured_at')
    currency = row['currency']
    text = []
    if 'intraday_budget' in signals:
        spend, budget = row['current']['spend'], row['verified_daily_budget']
        text.extend(f'{label} {number(value, money=True, currency=currency)}' for label, value in (
            ('今天已花', spend), ('当前日预算', budget), ('比当前日预算多', spend - budget),
        ))
    elif 'intraday_no_clicks' in signals:
        current = row['current']
        text.extend(f'{label} {number(current[field])}' for label, field in (
            ('今天曝光', 'impressions'), ('今天点击', 'clicks'),
        ))
        text.append('今天已花 ' + number(current['spend'], money=True, currency=currency))
    else:
        current, mature = row['current'], row['mature']
        if TRAFFIC.intersection(signals):
            label = ('日报花费 ' + row['report_date']) if include_dates else '日报花费'
            text.append(label + ' ' + number(current['spend'], money=True, currency=currency))
            traffic = [f'{name} {number(current[field])}' for field, name in (
                ('clicks', '点击'), ('impressions', '曝光')) if current.get(field) is not None]
            if traffic:
                text.append(' · '.join(traffic))
        if set(signals) - TRAFFIC:
            label = ('成熟花费 ' + row['mature_date']) if include_dates else '成熟花费'
            text.append(label + ' ' + number(mature['spend'], money=True, currency=currency))
            text.append('点击 ' + number(mature['clicks']) + ' · 归因订单 ' + number(mature['orders']))
            expected = (row.get('mature_comparison') or {}).get('expected_orders_at_reference_rate')
            if 'zero_conversion' in signals and type(expected) in (int, float):
                text.append(f'按参考转化率估算 {expected:.1f} 单')
        for code in signals:
            metric = row['kpis'].get(code, {})
            a, b = metric.get('current'), metric.get('baseline')
            if a is not None and b is not None:
                reference = {'SEVEN_DAY': '前七日', 'PREVIOUS_DAY': '前一日'}.get(
                    metric.get('comparison_basis'), '参考')
                label = _RATE_LABELS.get(code, code)
                delta = f'，低 {(b - a) * 100:.2f} 个百分点' if code in _RATE_LABELS and b > a else ''
                text.append(f'{label} {_metric(a, code, currency)}（{reference} {_metric(b, code, currency)}{delta}）')
        gap = ctr_expected_click_gap(row) if 'CTR' in signals else None
        if gap:
            text.append(f'按参考点击率估算少约 {gap:.0f} 次点击')
    checks = list(dict.fromkeys(_NEXT_CHECK[code] for code in signals if code in _NEXT_CHECK))
    headlines = [TITLES[code] for code in signals]
    basis = []
    if include_dates and intraday:
        headlines = [headline.replace('今天', '观测日') for headline in headlines]
        text = [line.replace('今天', '当日') for line in text]
        basis.append('日内观测（非最终日报） · 店铺当地 ' + plain(row.get('report_date'), 10))
        if observed_at:
            basis.append('采样时间 ' + plain(observed_at, 42))
    elif include_dates:
        if TRAFFIC.intersection(signals):
            basis.append('流量日报 · 店铺当地 ' + plain(row.get('report_date'), 10))
        if set(signals) - TRAFFIC:
            window = row.get('attribution_days')
            suffix = f' · {window} 天归因' if type(window) is int and window > 0 else ''
            basis.append('转化成熟日 · 店铺当地 ' + plain(row.get('mature_date'), 10) + suffix)
    if source:
        basis.append('来源：领星广告日内观测' if intraday else '来源：领星广告日报')
    return {field: row[field] for field in ('store_id', 'ad_type', 'campaign_id')} | {
        'store': plain(row['store_name'], 64), 'campaign': plain(row['campaign_name'], 105),
        'signal': '；'.join(headlines), 'numbers': '\n'.join(text),
        'basis': '\n'.join(basis),
        'next_check': '；'.join(checks[:2]),
        **({'live_observation': {'day': row.get('report_date'), 'captured_at': observed_at,
            'daily_budget': row.get('verified_daily_budget') if 'intraday_budget' in signals else None}}
           if intraday and source == 'lingxing_sp_intraday_observation' else {}),
    }


def _advertising_card_with_items(items, *, title='广告异常', store_mentions=None):
    def render(rows):
        has_basis = any('basis' in i for i in rows)
        checks = [(i['store'] + '\n' + i['campaign']
                   + (f"\n{i['ad_type'].upper()} · 活动 {i['campaign_id']}" if 'basis' in i else ''),
            i['signal'] + ('\n先查 ' + i['next_check'] if i.get('next_check') else ''),
            '\n'.join(part for part in (i.get('basis'), i['numbers']) if part)) for i in rows]
        notes = [f'另有 {len(items) - len(rows)} 项异常已保留记录。'] if len(rows) < len(items) else []
        scope = f"{len({i['store_id'] for i in rows})} 家店铺 · {len(rows)} 个活动待复核"
        headers = ['店铺／活动', '问题与先查项', '证据／日期'] if has_basis else ['店铺／活动', '异常', '关键数据']
        value = card(title, subtitle=scope, sections=[('', headers, checks)], notes=notes, color='orange')
        if not has_basis:
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
    return result, list(visible)


def advertising_card(items, *, title='广告异常', store_mentions=None):
    return _advertising_card_with_items(items, title=title, store_mentions=store_mentions)[0]


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


def _live_observation_current(item, campaign, store, now):
    evidence = item.get('live_observation')
    if evidence is None:
        return True  # Frozen drafts from earlier versions have no new observation metadata.
    try:
        captured = datetime.fromisoformat(evidence['captured_at'])
        report_day = now.astimezone(ZoneInfo(store.timezone)).date().isoformat()
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return False
    if captured.tzinfo is None or not timedelta(0) <= now - captured <= timedelta(minutes=75):
        return False
    budget = evidence.get('daily_budget')
    return evidence.get('day') == report_day and (budget is None or budget == campaign.budget)


def eligible_items(db, settings, items, now, *, refreshed_since=None):
    ids = {campaign_identity(i) for i in items}
    if not ids:
        return []
    skills = load_ad_review_skills(settings)
    if skills.status == 'invalid':
        return []
    clock = now.astimezone(timezone.utc).replace(tzinfo=None)
    earliest = clock - timedelta(minutes=90)
    if refreshed_since is not None:
        earliest = max(earliest, refreshed_since.astimezone(timezone.utc).replace(tzinfo=None))
    controls = notification_states(settings, {item[0] for item in ids})
    ready = {}
    for campaign, store in db.execute(select(Campaign, Store).join(Store, Store.sid == Campaign.store_id).where(
            Store.status == 1, Campaign.state == 'enabled',
            Campaign.updated_at.between(earliest, clock),
            tuple_(Campaign.store_id, Campaign.campaign_type, Campaign.campaign_id).in_(ids))):
        ready[(campaign.store_id, campaign.campaign_type, campaign.campaign_id)] = (campaign, store)
    return [i for i in items if campaign_identity(i) in ready and controls[i['store_id']][0]
            and _live_observation_current(i, *ready[campaign_identity(i)], now)
            and i.get('skill_fingerprint') == skill_fingerprint(skills.for_campaign(
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
    reserved = subscriber_reservations(db, (i.get('incident_key') for i in items), lead[0])
    private_items = [i for i in items if i.get('incident_key') not in reserved]
    if not private_items:
        return [primary]
    copy = _write_batch(db, bucket, lead[0], private_items, day, group=group, routing=routing, overnight=overnight)
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
    for finding in sorted(findings, key=lambda f: f['evidence'].get('intraday') is not True):
        evidence = finding['evidence']
        identity = campaign_identity(evidence)
        if identity in candidates:
            continue
        signals = repeat_eligible_signals(evidence, finding['signals'], history, now)
        if not signals:
            continue
        if not incident_already_recorded(db, evidence, now=now):
            profile = evidence.get('ad_skill') or skills.for_campaign(*identity)
            incident = incident_metadata(evidence, signals)
            item = alert_item({**finding, 'signals': signals}, include_dates=True) | {
                'detected_at': now.isoformat(), 'skill_fingerprint': skill_fingerprint(profile),
                'incident_key': incident['incident_key'],
            }
            candidates[identity] = incident, item
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
    items = eligible_items(db, settings, [job.metadata_json['hourly_item'] for job in held], now,
                           refreshed_since=datetime.combine(day, OPENS, BEIJING))
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


def recheck_hourly_delivery(db, settings, payload, now):
    """Return the outgoing card and precisely the activities shown on it."""
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
    return _advertising_card_with_items(rows,
        title='夜间广告异常汇总' if payload['hourly_overnight'] else '广告异常', store_mentions=mentions)
