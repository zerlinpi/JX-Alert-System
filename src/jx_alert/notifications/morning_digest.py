"""Requested daily health brief and evidence-backed SP exceptions, without normal-ad noise."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jx_alert.ai.ad_review_evidence import automatic_sp_signals, build_ad_review_context
from jx_alert.ai.ad_review_skills import load_ad_review_skills, skill_fingerprint
from jx_alert.analysis.ad_kpis import compare_kpis
from jx_alert.analysis.evaluator import _ad_entity_events, _comparison_evidence
from jx_alert.analysis.materiality import material_change
from jx_alert.analysis.rules import RuleConfig
from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.notifications.ad_repeat import (
    INCIDENT, incident_already_recorded, incident_metadata, load_incident_history, repeat_eligible_signals,
)
from jx_alert.notifications.cards import card, compact_ad_kpi_rows, number, plain
from jx_alert.notifications.hourly_ads import advertising_card, alert_item
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.store_health import card_fits
from jx_alert.storage.collection_attempts import collection_issue_for_date
from jx_alert.storage.models import Campaign, DataCoverage, PerformanceNotice, SyncJob


def _date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def _config(settings):
    defaults = RuleConfig()
    return RuleConfig(**{field: getattr(settings, 'alert_' + field, getattr(defaults, field))
                         for field in defaults.__dataclass_fields__})


def sp_campaign_exceptions(db, settings, store, target):
    """Read every SP activity; keep dates/maturity and actual materiality gates."""
    target = _date(target)
    problem = collection_issue_for_date(db, store.sid, target)
    coverage = db.scalar(select(DataCoverage).where(DataCoverage.store_id == store.sid,
        DataCoverage.dimension == 'campaign_sp', DataCoverage.metric_date == target))
    if store.status != 1:
        return [], '授权或同步状态异常，无法完成广告判断'
    if problem == 'DAILY_FAILED':
        return [], '目标日报采集失败或尚未完成，无法判断广告'
    if coverage is None or coverage.status != 'COMPLETE':
        return [], '目标日报尚未完整采集，无法判断广告'
    days = [target - timedelta(days=i) for i in range(15)]
    values, currencies, covered = _comparison_evidence(db, store.sid, 'campaign_sp', days)
    if not values or not any(day == target for entries in values.values() for _, day in entries):
        return [], '商品推广接口无记录，无法判断广告'
    campaigns = list(db.scalars(select(Campaign).where(Campaign.store_id == store.sid,
        Campaign.campaign_type == 'sp').order_by(Campaign.campaign_id)))
    observed_entities = {entity for entries in values.values() for entity, day in entries if day == target}
    quality = []
    if coverage.row_count > len(observed_entities):
        return [], '商品推广日报记录不完整，无法判断广告'
    missing_masters = observed_entities - {c.campaign_id for c in campaigns}
    if missing_masters:
        quality.append('部分活动资料缺失')
    config, findings, incomplete, invalid = _config(settings), [], 0, 0
    titles = {'orders_decrease': '成熟归因订单下降', 'spend_no_conversion': '成熟广告有花费但无归因订单',
              'low_spend_no_conversion': '成熟广告有花费但无归因订单'}
    for campaign in campaigns:
        entity = campaign.campaign_id
        by_day = {}
        for metric, observations in values.items():
            for (candidate, on), value in observations.items():
                if candidate == entity and on in covered:
                    item = by_day.setdefault(on, {})
                    item[metric] = value
                    if metric in {'spend', 'sales'}:
                        item['currency' if metric == 'spend' else 'sales_currency'] = currencies[metric].get((entity, on))
        if not by_day:
            continue
        current = by_day.get(target, {})
        if entity in observed_entities and not all(isinstance(current.get(metric), (int, float))
                and math.isfinite(current[metric]) and current[metric] >= 0
                for metric in ('spend', 'clicks', 'impressions')):
            invalid += 1
            continue
        enabled = (campaign.state or '').lower() == 'enabled'
        if not enabled and not by_day.get(target, {}).get('spend'):
            continue
        detected, known, *_ = _ad_entity_events(values=values, currencies=currencies, covered=covered,
            entity_id=entity, report_date=target, attribution_days=7, campaign_enabled=enabled,
            daily_budget=campaign.budget if (campaign.budget_type or '').lower() == 'daily' else None, config=config)
        if not known and entity in observed_entities:
            incomplete += 1
        topics = []
        for event in detected:
            metric = {'orders_decrease': 'orders', 'spend_no_conversion': 'zero_conversion',
                      'low_spend_no_conversion': 'zero_conversion'}.get(event['rule'])
            on = event['metrics'].get('attribution_complete_report_date')
            if metric and on and material_change(by_day, on, metric):
                topics.append((titles[event['rule']], on))
        # An actual source over-budget event also needs verified material daily spend.
        today = by_day.get(target, {})
        if (campaign.last_over_budget_at and str(campaign.last_over_budget_at).startswith(target.isoformat())
                and (campaign.budget_type or '').lower() == 'daily' and campaign.budget
                and math.isfinite(campaign.budget) and campaign.budget > 0 and today.get('currency') == 'USD'
                and isinstance(today.get('spend'), (int, float))
                and math.isfinite(today['spend'])
                and today['spend'] >= campaign.budget * 1.2 and today['spend'] - campaign.budget >= 30):
            topics.append(('活动出现实质超预算记录', target.isoformat()))
        kpis = compare_kpis(by_day, target, maturity_days=7, store_scope=False,
            min_clicks=getattr(settings, 'alert_min_clicks', 10), min_impressions=getattr(settings, 'alert_kpi_min_impressions', 1000),
            min_spend=getattr(settings, 'alert_min_spend', 20), ratio=getattr(settings, 'alert_kpi_change_ratio', .15))
        for code, item in kpis.items():
            if item['status'] == 'ADVERSE' and material_change(by_day, item['current_date'], code):
                topics.append((item['title'], item['current_date']))
            elif item['status'] == 'ADVERSE':
                item['status'] = 'WATCH'
        if not topics:
            continue
        current = by_day.get(target, {})
        mature = by_day.get(target - timedelta(days=7), {})
        findings.append({'store_id': store.sid, 'store_name': store.name, 'campaign_id': entity,
            'campaign_name': campaign.name or entity, 'state': '投放中' if enabled else '当前未投放',
            'report_date': target.isoformat(), 'mature_date': (target - timedelta(days=7)).isoformat(),
            'topics': list(dict.fromkeys(topics)), 'spend': current.get('spend'), 'currency': current.get('currency'),
            'mature_spend': mature.get('spend'), 'mature_currency': mature.get('currency'),
            'mature_orders': mature.get('orders'), 'kpis': kpis})
    if incomplete:
        quality.append(f'{incomplete}个活动缺少完整基线或成熟归因证据')
    if invalid:
        quality.append(f'{invalid}个活动目标日关键指标不完整')
    return findings, '；'.join(quality) or None


def health_notice_summary(db, store, snapshot):
    """Pending notices are observations, not proof that every notice is a violation."""
    notices = list(db.scalars(select(PerformanceNotice).where(PerformanceNotice.store_id == store.sid,
        PerformanceNotice.status_label == 'PENDING').order_by(PerformanceNotice.last_seen.desc())))
    violations = [n for n in notices if re.search(
        r'违规|侵权|假冒|停权|违反政策|policy\s+violation|infringement|counterfeit|account\s+suspend', n.subject or '', re.I)]
    if violations:
        label = f'归档观测：{len(violations)}条违规相关通知待核查'
    elif notices:
        label = f'{len(notices)}条通知待处理，性质待核查'
    elif snapshot['notice_freshness_status'] == 'FRESH':
        label = '未见待处理违规通知'
    else:
        # An empty, undated archive is not a useful positive assurance. The
        # page-level scope note preserves that distinction without boilerplate
        # for every store.
        label = None
    relevant = violations or notices
    if relevant:
        subjects = list(dict.fromkeys(re.sub(r'\s+', ' ', n.subject or '').strip()[:60]
                                     for n in relevant if (n.subject or '').strip()))
        if subjects:
            label += '：' + '；'.join(subjects[:2])
    return label


def morning_health_row(db, settings, store, *, now=None, proactive=False):
    health = store_health_snapshot(db, store, settings, now=now)
    if proactive:
        # Automatic messages contain actual current observations, never a
        # request to repair authorization, missing data or an old source.
        clock = now or datetime.now(timezone.utc)
        try:
            captured = datetime.fromisoformat(health['captured_at'])
        except (TypeError, ValueError):
            return None
        if (store.status != 1 or health['status'] in {'AUTHORIZATION_FAILED', 'INACTIVE', 'NO_DATA'}
                or health['freshness_status'] in {'FAILED', 'MISSING', 'STALE', 'FUTURE'}
                or captured > clock or clock - captured > timedelta(
                    hours=getattr(settings, 'lingxing_performance_max_age_hours', 8))):
            return None
    notice = health_notice_summary(db, store, health)
    archived_notice = ('\n' + notice) if notice and health['pending_notice_observations'] else ''
    # Keep operational blockers once, with the action that can resolve them.
    # Old observations must not obscure a store whose authorization now fails.
    if health['status'] == 'AUTHORIZATION_FAILED':
        return store.name, '店铺授权失效', '在领星重新授权' + archived_notice
    if health['status'] == 'INACTIVE':
        return store.name, ('领星欠费停服' if store.status == 3 else '领星已停止同步'), '恢复同步后复核' + archived_notice
    if health['status'] == 'NO_DATA':
        return store.name, ('绩效采集失败' if health['freshness_status'] == 'FAILED' else '未取得绩效数据'), '同步绩效后复核' + archived_notice

    observed = []
    if health['ahr_score'] is not None:
        observed.append(f"评分 {number(health['ahr_score'])}")
    if health['source_status_label'] not in {'未提供', '未识别', '不适用'}:
        observed.append(health['source_status_label'])
    if proactive and not observed:
        observed = [f"{r['label']} {r['value']:g}%" for r in health['indicators']
                    if r['value'] is not None][:2]
        if not observed:
            return None
    summary = ' · '.join(observed) or '评级未提供'
    if health['status'] != 'HEALTHY':
        summary += '（观测）'
    attention = [notice] if notice else []
    for indicator in health['indicators']:
        if indicator['status'] in {'BREACHED', 'AT_THRESHOLD'}:
            boundary = '，达到边界' if indicator['status'] == 'AT_THRESHOLD' else ''
            attention.append(f"{indicator['label']} {indicator['value']:g}%（标准 {indicator['standard']:g}%{boundary}）")
    issue = {'STALE': '绩效更新滞后', 'FUTURE': '绩效源时间异常', 'FAILED': '绩效采集失败'}.get(health['freshness_status'])
    if issue:
        attention.append(issue)
    return store.name, summary, '\n'.join(attention) or '—'


def _health_chunks(db, settings, stores, send_day, now, *, proactive=False):
    rows = [(store.sid, morning_health_row(db, settings, store, now=now, proactive=proactive)) for store in stores]
    rows = [(sid, row) for sid, row in rows if row is not None]
    result, offset = [], 0
    while offset < len(rows):
        size = min(10, len(rows) - offset)
        while size:
            batch = [r[1] for r in rows[offset:offset + size]]
            notes = []
            if not proactive and any('（观测）' in row[1] for row in batch):
                notes.append('观测值未确认当前健康；违规仅列已归档待处理项。')
            value = card(f'店铺绩效与违规快报 · {send_day}', subtitle='账户绩效与需处理事项',
                sections=[('店铺绩效', ['店铺', '绩效观测', '待办与风险'],
                    batch)], notes=notes)
            if card_fits(value):
                break
            size -= 1
        if not size:
            raise ValueError('Morning health row exceeds the bounded card limit')
        result.append((value, [r[0] for r in rows[offset:offset + size]]))
        offset += size
    return result


def select_morning_dates(db, stores, dates, warnings=None, history_allowed=None):
    """Reuse the existing exact-date/failure/history policy before freezing pages."""
    from jx_alert.notifications.daily_report import _report_selection
    actual, warnings = {}, dict(warnings or {})
    for store in stores:
        day, warning, _, _ = _report_selection(db, store, _date(dates[store.sid]), warnings.get(store.sid),
            scheduled=True, allow_history=store.sid in (history_allowed or set()))
        actual[store.sid] = day
        if warning:
            warnings[store.sid] = warning
    return actual, warnings


def _business_chunks(db, settings, stores, dates, warnings, target_dates, reporting_day, *,
                     proactive=False, business_pages=None):
    from jx_alert.notifications.business_daily import business_daily_card, grouped_business_pages

    reports = []
    for store in stores:
        actual = _date(dates[store.sid])
        target = _date((target_dates or dates)[store.sid])
        if proactive and actual != target:
            continue
        value = business_daily_card(db, store, actual, settings=settings,
            data_warning=None if proactive else (warnings or {}).get(store.sid), reporting_day=reporting_day)
        if value is None:
            continue
        if actual != target:
            value['body']['elements'].append({'tag': 'markdown',
                'content': f'目标报表日 {target}；展示历史报表日 {actual}。'})
        if not proactive and not card_fits(value):
            raise ValueError('Morning business report exceeds the card limit')
        reports.append((store.sid, value))
    if not proactive:
        return [(value, [sid]) for sid, value in reports]
    chunks = []
    for value, store_ids, sections in grouped_business_pages(reports, reporting_day):
        chunks.append((value, store_ids))
        if business_pages is not None:
            business_pages.append(sections)
    return chunks


def _proactive_ad_chunks(db, settings, stores, dates, warnings, target_dates, now, incident_pages):
    targets = target_dates or dates
    eligible = [s for s in stores if not (warnings or {}).get(s.sid)
                and _date(dates[s.sid]) == _date(targets[s.sid])]
    skills = load_ad_review_skills(settings)
    if skills.status == 'invalid':
        return []
    context = build_ad_review_context(db, settings, eligible, dates, now=now, skills=skills)
    clock = now or datetime.now(timezone.utc)
    history = load_incident_history(db, clock)
    items = []
    for evidence in context['campaigns']:
        signals = repeat_eligible_signals(evidence, automatic_sp_signals(evidence), history, clock)
        if signals and not incident_already_recorded(db, evidence, now=clock):
            item = alert_item({'evidence': evidence, 'signals': signals}, include_dates=True)
            state = evidence['current_state']
            if state != '状态未确认':
                item['campaign'] += '（' + state + '）'
            incident = incident_metadata(evidence, signals)
            identity = (incident['store_id'], incident['ad_type'], incident['campaign_id'])
            check = (*identity, skill_fingerprint(evidence.get('ad_skill') or skills.for_campaign(*identity)))
            items.append((item, (incident, check)))
    chunks = []
    for offset in range(0, len(items), 10):
        batch, incidents = zip(*items[offset:offset + 10])
        value = advertising_card(batch, title='商品推广活动待复核')
        chunks.append((value, list(dict.fromkeys(row['store_id'] for row in batch))))
        if incident_pages is not None:
            incident_pages.append(incidents)
    return chunks


def morning_cards(db, settings, stores, dates, *, send_day, now=None, warnings=None, target_dates=None,
                  include_quality=True, include_business=True, proactive=False, incident_pages=None, business_pages=None):
    """Return health, verified business reports, then material SP exceptions."""
    if proactive:
        stores = [s for s in stores if s.status == 1]
    chunks = (_health_chunks(db, settings, stores, send_day, now, proactive=True) if proactive else
              _health_chunks(db, settings, stores, send_day, now))
    if include_business:
        chunks.extend(_business_chunks(db, settings, stores, dates, warnings, target_dates,
                                      _date(send_day) - timedelta(days=1), proactive=proactive,
                                      business_pages=business_pages))
    if proactive:
        chunks.extend(_proactive_ad_chunks(db, settings, stores, dates, warnings, target_dates, now, incident_pages))
        return chunks
    pending = []
    for store in stores:
        target = _date((target_dates or dates)[store.sid])
        actual = _date(dates[store.sid])
        findings, quality = sp_campaign_exceptions(db, settings, store, dates[store.sid])
        if (warnings or {}).get(store.sid):
            quality = (warnings or {})[store.sid]
            findings = []
        if actual != target:
            note = f'目标日尚未采集，仅复核历史报表日{actual}，不代表目标日数据'
            quality = note + ('；' + quality if quality else '')
        if quality and include_quality:
            pending.append((store.sid, (store.name, str(target), quality)))
        for finding in findings:
            topics = '；'.join(title + '（' + day + '）' for title, day in finding['topics'])
            dates_and_values = [('实际复核日', finding['report_date']),
                ('花费', number(finding['spend'], money=True, currency=finding['currency'] or '币种待确认')),
                ('成熟归因日', finding['mature_date']),
                ('成熟花费', number(finding['mature_spend'], money=True,
                    currency=finding['mature_currency'] or '币种待确认')),
                ('成熟归因订单', number(finding['mature_orders']))]
            if actual != target:
                dates_and_values.insert(0, ('目标报表日', target.isoformat()))
            value = card('商品推广活动待复核', subtitle=store.name + ' · ' + finding['campaign_name'],
                sections=[('异常内容', ['活动状态', '问题'], [(finding['state'], topics)]),
                    ('关键数据', ['指标', '数值'], dates_and_values),
                    ('广告指标', ['指标', '本期/日期', '参考/变化', '判断'], compact_ad_kpi_rows(finding['kpis']))])
            if not card_fits(value):
                raise ValueError('Morning exception exceeds the card limit')
            chunks.append((value, [store.sid]))
    for offset in range(0, len(pending), 10):
        batch = pending[offset:offset + 10]
        value = card('广告判断待补齐数据', subtitle='以下缺口不能视为广告正常',
            sections=[('影响判断的数据缺口', ['店铺', '目标报表日', '待确认项'], [r[1] for r in batch])])
        if not card_fits(value):
            raise ValueError('Morning data-quality page exceeds the card limit')
        chunks.append((value, [r[0] for r in batch]))
    return chunks


def enqueue_morning_digest(db, settings, stores, dates, warnings, *, send_day, due, delivery, now, history_allowed=None):
    chat_id = delivery['receive_id']
    # Reuse ANY format already frozen for this delivery day; never resend on upgrade.
    existing = list(db.scalars(select(NotificationOutbox).where(
        NotificationOutbox.idempotency_key.startswith('scheduled-daily:'), NotificationOutbox.chat_id == chat_id,
        NotificationOutbox.payload['scheduled_delivery_date'].as_string() == send_day.isoformat()).order_by(NotificationOutbox.id)))
    if existing:
        return {'queued': len(existing), 'skipped': False, 'outbox_ids': [r.id for r in existing]}
    prefix = 'scheduled-daily:morning:' + hashlib.sha256(f'{send_day}:{chat_id}'.encode()).hexdigest() + ':'
    actual, warnings = select_morning_dates(db, stores, dates, warnings, history_allowed)
    incident_pages, business_pages = [], []
    chunks = morning_cards(db, settings, stores, actual, send_day=send_day, now=now,
        warnings=warnings, target_dates=dates, include_quality=False, proactive=True, incident_pages=incident_pages,
        business_pages=business_pages)
    incident_pages, business_pages = iter(incident_pages), iter(business_pages)
    ids = []
    try:
        with db.begin_nested():
            for index, (value, store_ids) in enumerate(chunks):
                title = plain(value['header']['title']['content'])
                kind = ('health' if title.startswith('店铺绩效与违规快报') else
                        'ad_exception' if title == '商品推广活动待复核' else
                        'business_daily' if '经营日报' in title else 'data_quality')
                incident_pairs = next(incident_pages, ()) if kind == 'ad_exception' else ()
                business_sections = next(business_pages, ()) if kind == 'business_daily' else ()
                row = NotificationOutbox(idempotency_key=prefix + str(index), chat_id=chat_id,
                    next_attempt_at=due.astimezone(timezone.utc).replace(tzinfo=None), payload={
                        'text': title, 'store_ids': store_ids,
                        'store_id': store_ids[0] if len(store_ids) == 1 else None,
                        'store_report_dates': {sid: {'target': str(dates[sid]), 'actual': str(actual[sid])} for sid in store_ids},
                        'open_ids': [], 'fixed_route': True, 'transport': 'app', 'delivery': delivery, 'card': value,
                        'scheduled_delivery_date': send_day.isoformat(), 'scheduled_timezone': 'Asia/Shanghai',
                        'display_reporting_date': (send_day - timedelta(days=1)).isoformat(),
                        'scheduled_not_before': due.isoformat(), 'morning_focus': True, 'morning_message_kind': kind,
                        'quiet_verified': True, 'morning_batch': prefix,
                        **({'business_sections': business_sections} if business_sections else {}),
                        **({'ad_group_owners': True, 'ad_skill_checks': [check for _, check in incident_pairs]}
                           if kind == 'ad_exception' else {})})
                db.add(row)
                db.flush()
                ids.append(row.id)
                if kind == 'ad_exception':
                    for incident, _ in incident_pairs:
                        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
                            started_at=now.astimezone(timezone.utc).replace(tzinfo=None),
                            metadata_json={**incident, 'outbox_ids': [row.id], 'origin': 'daily_report'}))
    except IntegrityError:
        ids = list(db.scalars(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key.startswith(prefix))))
        if not ids:
            raise
    return {'queued': len(ids), 'skipped': not bool(ids), 'outbox_ids': ids}
