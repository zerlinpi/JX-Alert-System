"""Recipient-scoped, evidence-backed advertising batches and Beijing quiet hours."""
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_

from jx_alert.ai.ad_review_evidence import ctr_expected_click_gap
from jx_alert.ai.ad_review_skills import load_ad_review_skills, skill_fingerprint
from jx_alert.analysis.hourly_ad_patterns import HIGH_LOW, LOW_ZERO, OPPORTUNITY, PATTERN_SIGNALS
from jx_alert.notifications.hourly_ad_routing import (
    ROLE as SCOPED_ROLE, app_fingerprint, assignment, private_destination, scoped_app, scoped_routing_enabled, scoped_routing_file,
)
from jx_alert.notifications.ad_repeat import (
    INCIDENT, campaign_identity, incident_already_recorded, incident_metadata,
    load_incident_history, repeat_eligible_signals, subscriber_reservations,
    live_observation_current as _live_observation_current,
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
    HIGH_LOW: '高花费 · 成熟转化偏低',
    LOW_ZERO: '连续多日小额花费 · 成熟归因仍无订单',
    OPPORTUNITY: '低花费 · 成熟转化表现较好（观察机会）',
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
    HIGH_LOW: '同活动获客效率及花费分布',
    LOW_ZERO: '多日花费及归因转化表现',
    OPPORTUNITY: '观察能否持续，未建议加预算',
}


def _routing(settings):
    try:
        reader = scoped_routing_file if scoped_routing_enabled(settings) else _load_routing
        return reader(settings.feishu_routing_file)
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
    pattern = row.get('hourly_pattern') or {}
    pattern_code = pattern.get('code')
    shown_pattern = pattern_code in PATTERN_SIGNALS and pattern_code in signals
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
        if shown_pattern:
            text.append('成熟期花费 ' + number(pattern['spend'], money=True, currency=currency))
            text.append('点击 ' + number(pattern['clicks']) + ' · 归因订单 ' + number(pattern['orders']))
            if pattern_code == OPPORTUNITY:
                text.append('每单花费 ' + number(pattern['cpa'], money=True, currency=currency)
                            + '；参考 ' + number(pattern['reference_cpa'], money=True, currency=currency))
                text.append(f"广告回报 {pattern['roas']:.2f} 倍；参考 {pattern['reference_roas']:.2f} 倍")
            else:
                text.append(f"按参考转化率约 {pattern['expected_orders']:.1f} 单（对照估算）")
                if pattern_code == HIGH_LOW:
                    text.append('较历史每单成本的估算差额 '
                                + number(pattern['estimated_excess_spend'], money=True, currency=currency)
                                + '（非实际亏损）')
        if set(signals) - TRAFFIC - PATTERN_SIGNALS:
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
        if shown_pattern:
            window = row.get('attribution_days')
            suffix = f' · {window} 天归因' if type(window) is int and window > 0 else ''
            for heading, dates in (('成熟取数', pattern['dates']), ('独立成熟对照', pattern['reference_dates'])):
                interval = ' 至 '.join(dict.fromkeys((dates[0], dates[-1])))
                basis.append(f'{heading} · 店铺当地 {interval}' + (suffix if heading == '成熟取数' else ''))
        if set(signals) - TRAFFIC - PATTERN_SIGNALS:
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
        **({'category': 'opportunity'} if pattern_code == OPPORTUNITY and shown_pattern else {}),
        **({'live_observation': {'day': row.get('report_date'), 'captured_at': observed_at,
            'daily_budget': row.get('verified_daily_budget') if 'intraday_budget' in signals else None}}
           if intraday and source == 'lingxing_sp_intraday_observation' else {}),
    }


def _advertising_card_with_items(items, *, title='广告异常', store_mentions=None, operator_labels=None):
    def render(rows):
        has_basis = any('basis' in i for i in rows)
        signals = {False: [], True: []}
        for i in rows:
            ident = i['store'] + '\n' + i['campaign']
            if 'basis' in i:
                ident += f"\n{i['ad_type'].upper()} · 活动 {i['campaign_id']}"
            if operator_labels and i['store_id'] in operator_labels:
                ident += '\n对应运营：' + operator_labels[i['store_id']]
            signals[i.get('category') == 'opportunity'].append((
                ident, i['signal'] + ('\n先查 ' + i['next_check'] if i.get('next_check') else ''),
                '\n'.join(part for part in (i.get('basis'), i['numbers']) if part)))
        notes = [f'另有 {len(items) - len(rows)} 个活动已保留记录。'] if len(rows) < len(items) else []
        scope = f"{len({i['store_id'] for i in rows})} 家店铺 · {len(rows)} 个活动待复核"
        headers = ['店铺／活动', '问题与先查项', '证据／日期'] if has_basis else ['店铺／活动', '异常', '关键数据']
        sections = [('', headers, signals[False])]
        heading = title
        if signals[True]:
            heading = title + '与优化机会' if signals[False] else '广告优化机会'
            sections.append(('优化机会 · 非浪费告警', ['店铺／活动', '观察项', '成熟证据／日期'], signals[True]))
        value = card(heading, subtitle=scope, sections=sections, notes=notes,
                     color='orange' if signals[False] else 'blue')
        if not has_basis:
            value['body']['elements'].pop(0)
        if store_mentions:
            mentions = list(dict.fromkeys(uid for row in rows for uid in store_mentions.get(row['store_id'], ())))
            value = with_mentions(value, mentions)
        return value

    # Keep one recipient message and use native table pagination within it.
    visible = sorted(items, key=lambda i: i.get('category') == 'opportunity')[:50]
    result = render(visible)
    while len(visible) > 1 and not card_fits(result):
        visible = visible[:-1]
        result = render(visible)
    return result, list(visible)


def advertising_card(items, *, title='广告异常', store_mentions=None, operator_labels=None):
    return _advertising_card_with_items(
        items, title=title, store_mentions=store_mentions, operator_labels=operator_labels)[0]

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


def _group_mentions(settings, routing, delivery, items, app_id):
    sids = {i['store_id'] for i in items}
    if not scoped_routing_enabled(settings):
        return {sid: report_group_owner_mentions(routing, delivery, sid) for sid in sids}
    group = routing.get('groups', {}).get(delivery.get('routing_alias'), {})
    if not app_id or group.get('hourly_verified_roster_app_id') != app_id:
        return {}
    return {sid: [proof['operator']] for sid in sids
            if (proof := assignment(routing, sid, app_id))
            and proof['operator'] in report_group_owner_mentions(routing, delivery, sid)}


def _write_batch(db, settings, bucket, destination, items, day, *, group, routing, overnight,
                 private_scopes=None, operator_labels=None, app_id=None):
    key = _key(bucket, destination)
    if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)):
        return None
    mentions = _group_mentions(settings, routing, group, items, app_id) if destination == group else None
    value = advertising_card(items, title='夜间广告异常汇总' if overnight else '广告异常',
                             store_mentions=mentions, operator_labels=operator_labels)
    row = enqueue_notification(db, idempotency_key=key, text=value['header']['title']['content'],
        chat_id=destination['receive_id'], store_ids=sorted({i['store_id'] for i in items}),
        fixed_route=True, delivery=destination, card=value, material_ad_alert=True)
    row.payload = {**row.payload, 'hourly_ad_monitor': True, 'hourly_items': items,
                   'hourly_delivery_date': day.isoformat(), 'hourly_overnight': overnight,
                   'hourly_group_delivery': group}
    if scoped_routing_enabled(settings):
        row.payload = {**row.payload, 'hourly_routing_version': 1,
                       'hourly_routing_app_identity': app_fingerprint(app_id)}
    if private_scopes is not None:
        row.payload = {**row.payload, 'hourly_private_scopes': private_scopes}
    return row.id

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
    if scoped_routing_enabled(settings):
        from jx_alert.operations import CollectionConfigurationError, _checked_scope
        try:
            scope = _checked_scope(settings, None)
        except CollectionConfigurationError:
            return []
        ids = {identity for identity in ids if identity[0] in scope}
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


def _group_eligible(settings, routing, items, app_id):
    if not scoped_routing_enabled(settings) or getattr(settings, 'feishu_group_fallback_enabled', False):
        return items
    return [item for item in items if assignment(routing, item['store_id'], app_id) is not None]


def _write_routes(db, settings, bucket, items, day, *, overnight=False):
    routing = _routing(settings)
    group = _group(settings, routing)
    scoped = scoped_routing_enabled(settings)
    app_id = scoped_app(settings) if scoped else None
    if scoped and overnight:
        # An old hold has no authority to migrate to a new report group.
        items = [item for item in items if item.get('hourly_hold_group') == group
                 and item.get('hourly_hold_app_identity') == app_fingerprint(app_id)]
    items = _group_eligible(settings, routing, items, app_id)
    if not group or not items:
        return []
    primary = _write_batch(db, settings, bucket, group, items, day, group=group, routing=routing,
                           overnight=overnight, app_id=app_id)
    if primary is None:
        # Keep late findings eligible for the next cycle rather than sending a
        # second group message or a private-only batch in this cycle.
        return []
    if scoped:
        scopes, names, by_recipient, by_store = {}, {}, {}, {}
        for sid in sorted({item['store_id'] for item in items}):
            proof = assignment(routing, sid, app_id)
            if proof:
                names[sid] = proof['operator_name']
                by_store[sid] = list(proof['fingerprints'])
                for uid, fingerprint in proof['fingerprints'].items():
                    scopes.setdefault(uid, {})[sid] = fingerprint
        for item in items:
            for uid in by_store.get(item['store_id'], ()):
                frozen = item.get('hourly_hold_private_binding', [])
                if not overnight or scopes[uid][item['store_id']] in frozen:
                    by_recipient.setdefault(uid, []).append(item)
        ids = [primary]
        for uid, eligible in by_recipient.items():
            destination = private_destination(uid)
            reserved = subscriber_reservations(db, (i.get('incident_key') for i in eligible), destination)
            selected = [item for item in eligible if item.get('incident_key') not in reserved]
            if selected:
                sids = {item['store_id'] for item in selected}
                copy = _write_batch(db, settings, bucket, destination, selected, day,
                    group=group, routing=routing, overnight=overnight,
                    private_scopes={sid: scopes[uid][sid] for sid in sids},
                    operator_labels={sid: names[sid] for sid in sids}, app_id=app_id)
                if copy is not None:
                    ids.append(copy)
        return ids
    lead = advertising_subscriber_deliveries(settings)
    if len(lead) != 1:
        return [primary]
    reserved = subscriber_reservations(db, (i.get('incident_key') for i in items), lead[0])
    private_items = [i for i in items if i.get('incident_key') not in reserved]
    if not private_items:
        return [primary]
    copy = _write_batch(db, settings, bucket, lead[0], private_items, day, group=group, routing=routing, overnight=overnight)
    return [primary, copy] if copy is not None else [primary]


def _delivery_incident_ids(db, ids, item):
    return [oid for oid in ids if any(i.get('incident_key') == item['incident_key']
            for i in db.get(NotificationOutbox, oid).payload['hourly_items'])]

def queue_hourly_findings(db, settings, findings, *, now, slot, send_alerts):
    if not settings.feishu_enabled or settings.feishu_transport != 'app' or not send_alerts:
        return []
    local = now.astimezone(BEIJING)
    day = local.date()
    overnight = not OPENS <= local.time() < CLOSES
    history = load_incident_history(db, now)
    skills = load_ad_review_skills(settings)
    candidates = {}
    for finding in sorted(findings, key=lambda f: (
            f.get('signals') == [OPPORTUNITY], f['evidence'].get('intraday') is not True)):
        evidence = finding['evidence']
        opportunity = finding.get('signals') == [OPPORTUNITY]
        evidence = {**evidence, 'category': 'opportunity'} if opportunity else evidence
        identity = campaign_identity(evidence)
        if identity in candidates:
            continue
        signals = repeat_eligible_signals(evidence, finding['signals'], history, now)
        if not signals:
            continue
        if not incident_already_recorded(db, evidence, now=now):
            profile = evidence.get('ad_skill') or skills.for_campaign(*identity)
            incident = incident_metadata(evidence, signals)
            item = alert_item({**finding, 'signals': signals, 'evidence': evidence}, include_dates=True) | {
                'detected_at': now.isoformat(), 'skill_fingerprint': skill_fingerprint(profile),
                'incident_key': incident['incident_key'],
            }
            candidates[identity] = incident, item
    valid = eligible_items(db, settings, [i for _, i in candidates.values()], now)
    ids = [] if overnight else _write_routes(db, settings, slot, valid, day)
    if not overnight and not ids:
        return []
    held_routing = _routing(settings) if overnight and scoped_routing_enabled(settings) else None
    held_group = _group(settings, held_routing) if held_routing is not None else None
    held_app = scoped_app(settings) if held_routing is not None else None
    for item in valid:
        identity = campaign_identity(item)
        item_ids = [] if overnight else _delivery_incident_ids(db, ids, item)
        if not overnight and not item_ids:
            continue
        meta = {**candidates[identity][0], 'outbox_ids': item_ids}
        if overnight:
            if held_routing is not None:
                proof = assignment(held_routing, item['store_id'], held_app)
                item = {**item, 'hourly_hold_group': held_group,
                        'hourly_hold_app_identity': app_fingerprint(held_app),
                        'hourly_hold_private_binding': sorted(proof['fingerprints'].values()) if proof else []}
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
    for job in held:
        item_ids = _delivery_incident_ids(db, ids, job.metadata_json['hourly_item'])
        if item_ids:
            job.status = 'QUEUED'
            job.metadata_json = {**job.metadata_json, 'outbox_ids': item_ids}
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
    role = destination.get('role')
    app_id = scoped_app(settings)
    fingerprint = app_fingerprint(app_id)
    if (payload.get('hourly_routing_version') == 1
            and payload.get('hourly_routing_app_identity') != fingerprint):
        return None  # queued cards may not switch Feishu applications
    if role == ROLE:
        if (scoped_routing_enabled(settings) or payload.get('hourly_group_delivery') != current
                or advertising_subscriber_deliveries(settings) != [destination]):
            return None
    elif role == SCOPED_ROLE:
        if (not fingerprint or not scoped_routing_enabled(settings) or payload.get('hourly_group_delivery') != current
                or not isinstance(payload.get('hourly_private_scopes'), dict)
                or not payload['hourly_private_scopes']
                or payload.get('transport') != 'app' or payload.get('fixed_route') is not True
                or destination != private_destination(destination.get('receive_id'))):
            return None
    elif current != destination:
        return None
    rows = eligible_items(db, settings, payload['hourly_items'], now)
    labels = None
    if role == SCOPED_ROLE:
        uid = destination['receive_id']
        labels = {sid: proof['operator_name'] for sid, fingerprint in payload['hourly_private_scopes'].items()
                  if (proof := assignment(routing, sid, app_id)) and proof['fingerprints'].get(uid) == fingerprint}
        rows = [item for item in rows if item['store_id'] in labels]
    elif destination == current:
        rows = _group_eligible(settings, routing, rows, app_id)
    if not rows:
        return None
    mentions = _group_mentions(settings, routing, current, rows, app_id) if destination == current else None
    return _advertising_card_with_items(rows,
        title='夜间广告异常汇总' if payload['hourly_overnight'] else '广告异常',
        store_mentions=mentions, operator_labels=labels)
