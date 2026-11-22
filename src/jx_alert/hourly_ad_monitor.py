"""Lightweight hourly SP observations and read-only analyst-reviewed notifications."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from jx_alert.ai.ad_review import review_advertising
from jx_alert.ai.ad_review_evidence import automatic_sp_signals, build_ad_review_context
from jx_alert.ai.ad_review_skills import load_ad_review_skills
from jx_alert.notifications.ad_repeat import (
    campaign_identity, incident_already_recorded, load_incident_history, repeat_eligible_signals,
)
from jx_alert.notifications.hourly_ads import queue_hourly_findings, queue_overnight_digest
from jx_alert.storage.coverage import store_sales_data_status
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, RawSnapshot, Store, SyncJob
from jx_alert.store_controls import collection_states


def _utcnow():
    return datetime.now(timezone.utc)


def _naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class _ReadCache:
    """Share this collection's account inventory without hiding owner switches."""
    def __init__(self, client, stores):
        self.client, self.stores, self.accounts = client, stores, None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_stores(self):
        return self.stores

    def seller_ad_accounts(self):
        if self.accounts is None:
            self.accounts = self.client.seller_ad_accounts()
        return self.accounts


def _ready_partition(db, sid, day, zone, *, maturity=0):
    row = db.scalar(select(DataCoverage).where(DataCoverage.store_id == sid,
        DataCoverage.metric_date == day, DataCoverage.dimension == 'campaign_sp'))
    deadline = datetime.combine(day + timedelta(days=maturity + 1), time.min, ZoneInfo(zone))
    return bool(row and row.status == 'COMPLETE' and row.completed_at is not None
                and row.completed_at.replace(tzinfo=timezone.utc) >= deadline.astimezone(timezone.utc))


def _morning_sales_status(db, sid, day):
    dimensions = ("store", "sku_asin", "sku_msku")
    partitions = {row.dimension: row for row in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == sid, DataCoverage.metric_date == day,
        DataCoverage.dimension.in_(dimensions),
    ))}
    facts = db.scalars(select(DailyMetric).where(
        DailyMetric.store_id == sid, DailyMetric.metric_date == day,
        DailyMetric.dimension == "store", DailyMetric.entity_id == sid,
    )).all()
    store_status = store_sales_data_status(1, partitions.get("store"), facts,
        partitions.get("sku_asin"), partitions.get("sku_msku"))
    partition_status = {
        dim: partitions[dim].status if dim in partitions else "NOT_COLLECTED"
        for dim in dimensions
    }
    return {"store_sales": store_status, "partitions": partition_status,
            "ready": store_status in {"AVAILABLE", "ZERO", "EMPTY"}
            and all(state == "COMPLETE" for state in partition_status.values())}


def _collect_store(client, db, settings, record, target, *, morning=False, now=None):
    from jx_alert.lingxing.campaign_collector import collect_campaigns
    from jx_alert.lingxing.collector import SPPartitionBusy, collect_ads_for_day
    from jx_alert.lingxing.ad_currency import account_currencies
    from jx_alert.lingxing.intraday_observation import run_observation
    from jx_alert.reporting.archive import archive_partitions

    sid, errors = str(record['sid']), []
    one = {'data': [record]}
    morning_ready = False
    sales_coverage = {}
    if morning:
        from jx_alert.lingxing.sales_collector import collect_sales_for_day
        from jx_alert.lingxing.api_business import collect_api_business_for_day
        from jx_alert.lingxing.business_dashboard import collect_business_for_day
        from jx_alert.lingxing.performance_openapi import collect_store_performance
        for day in (target, target - timedelta(days=1)):
            if not collection_states(settings, [sid]).get(sid):
                return {'status': 'DISABLED', 'errors': errors, 'sales_coverage': sales_coverage}
            local_day = day.isoformat()
            try:
                collect_sales_for_day(client, db, {sid: local_day}, stores_response=one)
                sales = _morning_sales_status(db, sid, day)
            except Exception as exc:
                db.rollback()
                sales = {'ready': False, 'collection': 'FAILED'}
                errors.append({'step': 'sales', 'date': local_day, 'error_type': type(exc).__name__})
            else:
                if not sales['ready']:
                    errors.append({'step': 'sales', 'date': local_day, 'error_type': 'IncompleteCoverage'})
                if day == target and sales['ready']:
                    morning_ready = True
            sales_coverage[local_day] = sales
        steps = [
            ('api_business', lambda: collect_api_business_for_day(client, db, settings, one, {sid: target.isoformat()})),
            ('business_dashboard', lambda: collect_business_for_day(db, settings, one, {sid: target.isoformat()})),
        ]
        if settings.lingxing_performance_enabled:
            steps.append(('performance', lambda: collect_store_performance(client, db,
                stores_response=one, settings=settings)))
        for name, collect in steps:
            if not collection_states(settings, [sid]).get(sid):
                return {'status': 'DISABLED', 'errors': errors}
            try:
                outcome = collect()
                if isinstance(outcome, dict) and outcome.get('status') in {'FAILED', 'PARTIAL_FAILED'}:
                    errors.append({'step': name, 'error_type': 'PartialSource'})
                if isinstance(outcome, dict) and outcome.get('status') in {'COMPLETE', 'SUCCEEDED', 'SUCCEEDED_WITH_WARNINGS'}:
                    morning_ready = True
            except Exception as exc:
                db.rollback()
                errors.append({'step': name, 'error_type': type(exc).__name__})
    sales_detail = {'sales_coverage': sales_coverage} if morning else {}
    if int(record.get('has_ads_setting') or 0) != 1:
        return {'status': 'PARTIAL' if errors else 'COMPLETE', 'errors': errors,
                'ads': 'UNAVAILABLE', 'ad_ready': False, 'intraday_ready': False,
                'morning_ready': morning_ready, **sales_detail}
    try:
        collect_campaigns(client, db, stores_response=one, report_types=('sp',))
    except Exception as exc:
        db.rollback()
        return {'status': 'PARTIAL', 'morning_ready': morning_ready, **sales_detail,
                'errors': [*errors, {'step': 'campaigns', 'error_type': type(exc).__name__}]}
    try:
        currencies = account_currencies(client.seller_ad_accounts())
    except Exception as exc:
        currencies = {}
        errors.append({'step': 'ad_accounts', 'error_type': type(exc).__name__})
    store = db.get(Store, sid)
    mature = target - timedelta(days=7)
    days = [target, mature]
    primary_ready = True
    for on, lag in ((target - timedelta(days=1), 0), (mature - timedelta(days=1), 7)):
        if not _ready_partition(db, sid, on, store.timezone, maturity=lag):
            days.append(on)
    reports = {}
    for on in dict.fromkeys(days):
        try:
            if not collection_states(settings, [sid]).get(sid):
                return {'status': 'DISABLED', 'errors': errors}
            response = collect_ads_for_day(client, db, {sid: on.isoformat()}, stores_response=one,
                ad_currencies=currencies, report_types=('sp',))
            count = response['rows']['sp']
            reports[on.isoformat()] = {'status': 'AVAILABLE' if count else 'EMPTY', 'row_count': count}
        except Exception as exc:
            db.rollback()
            if on in {target, mature}:
                primary_ready = False
            errors.append({'step': 'sp_report', 'date': on.isoformat(), 'error_type': type(exc).__name__})
            reports[on.isoformat()] = {'status': 'BUSY' if isinstance(exc, SPPartitionBusy) else 'FAILED'}
    try:
        clock = now or _utcnow()
        day = clock.astimezone(ZoneInfo(store.timezone)).date().isoformat()
        observed = run_observation(db, settings, {'store_ids': [sid], 'observation_date': day},
                                   client=client, now=clock)
    except Exception as exc:
        db.rollback()
        observed = {'error_type': type(exc).__name__}
        errors.append({'step': 'intraday', 'error_type': type(exc).__name__})
    archive = archive_partitions(db, settings, {sid: [d.isoformat() for d in days]}) if morning else None
    return {'status': 'PARTIAL' if errors else 'COMPLETE', 'errors': errors,
            'ad_ready': primary_ready, 'intraday_ready': observed.get('status') == 'COMPLETE',
            'observation': observed, 'excel': archive, 'morning_ready': morning_ready or primary_ready,
            'reports': reports, **sales_detail}


def run_hourly_monitor(*, now=None, settings=None, session_factory=None, warehouse_engine=None,
                       client_factory=None, analyst_factory=None, send_alerts=True):
    from jx_alert.config import get_settings
    from jx_alert.storage.db import SessionLocal, engine
    from jx_alert.storage.job_lock import database_job_lock
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError
    from jx_alert.operations import scoped_stores, sync_scoped_store_inventory, store_report_plan
    from jx_alert.storage.raw_archive import archive_raw_snapshots

    settings = settings or get_settings()
    if not settings.hourly_ad_monitor_enabled or not settings.scheduler_enabled or settings.app_dry_run:
        return {'status': 'DISABLED'}
    clock = now or _utcnow()
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError('Hourly monitor requires an aware clock')
    slot = clock.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    factory, warehouse_engine = session_factory or SessionLocal, warehouse_engine or engine
    kind = 'hourly_ad_monitor' if send_alerts else 'hourly_ad_monitor_validation'
    with database_job_lock(warehouse_engine, 'hourly_ad_monitor'), factory() as db:
        job = db.scalar(select(SyncJob).where(SyncJob.job_type == kind,
            SyncJob.metadata_json['slot'].as_string() == slot).order_by(SyncJob.id.desc()).limit(1))
        if job and job.status == 'SUCCEEDED':
            return {'status': 'ALREADY_COMPLETE', 'job_id': job.id}
        meta = deepcopy(job.metadata_json or {}) if job else {'slot': slot, 'attempts': 0, 'stores': {}}
        if job and job.status == 'RUNNING' and not meta.get('resumed_interruption'):
            # The prior owner has lost the database lock. Resume once without
            # charging the slot for an attempt that never recorded an outcome.
            meta['attempts'] = max(0, meta['attempts'] - 1)
            meta['resumed_interruption'] = True
            meta.pop('retry_at', None)
        if meta.get('retry_at') and clock < datetime.fromisoformat(meta['retry_at']):
            return {'status': 'WAITING', 'job_id': job.id}
        if meta['attempts'] >= 3:
            if job.status == 'RUNNING':
                job.status = 'PARTIAL_FAILED'
                job.finished_at = _naive(now or _utcnow())
                meta.update(error_type='Interrupted', retry_at=None)
                job.metadata_json = deepcopy(meta)
                db.commit()
            return {'status': 'RETRY_LIMIT', 'job_id': job.id}
        if job is None:
            job = SyncJob(job_type=kind, started_at=_naive(clock), metadata_json=meta)
            db.add(job)
        job.status, job.finished_at = 'RUNNING', None
        meta['attempts'] += 1
        job.metadata_json = deepcopy(meta)
        db.commit()
        job_id = job.id
        ready, intraday_snapshots, dates, morning_dates = [], {}, {}, {}
        morning = clock.astimezone(ZoneInfo('Asia/Shanghai')).hour == 8

        def freeze_morning():
            if morning and send_alerts and 'prepared_daily_report' not in meta and morning_dates:
                from jx_alert.operations import queue_scheduled_daily_reports
                db.rollback()
                meta['prepared_daily_report'] = queue_scheduled_daily_reports(
                    now=now or _utcnow(), prepare=True, prepared_dates=morning_dates)
                job = db.get(SyncJob, job_id)
                job.metadata_json = deepcopy(meta)
                db.commit()

        phase = 'raw_archive_checkpoint'
        try:
            if 'raw_start' not in meta:
                meta['raw_start'] = db.scalar(select(func.max(RawSnapshot.id))) or 0
            phase = 'inventory'
            try:
                client = (client_factory or LingxingClient.from_settings)()
                inventory = client.get_stores()
            except Exception:
                if client_factory is None and 'fallback' not in meta:
                    from jx_alert.hourly_source_fallback import collect_fallback_observations
                    db.rollback()
                    meta['fallback'] = collect_fallback_observations(db, settings, now=clock, morning=morning)
                    morning_dates.update(meta['fallback'].get('morning_dates', {}))
                    freeze_morning()
                raise
            selected = scoped_stores(inventory, settings.collection_scope_file, allow_missing=True)
            sync_scoped_store_inventory(db, settings, selected)
            db.commit()
            phase = 'store_collection'
            cached = _ReadCache(client, inventory)
            controls = collection_states(settings, [str(r['sid']) for r in selected['data']])
            plan = {p['store_id']: p for p in store_report_plan(selected['data'], settings.store_timezones_file, now=clock)}
            for record in selected['data']:
                sid = str(record['sid'])
                if not controls.get(sid) or plan[sid]['state'] != 'READY':
                    continue
                target = date.fromisoformat(plan[sid]['report_date'])
                previous = meta['stores'].get(sid, {})
                if previous.get('status') == 'COMPLETE':
                    result = previous
                else:
                    try:
                        result = _collect_store(cached, db, settings, record, target, morning=morning, now=now)
                    except Exception as exc:
                        db.rollback()
                        result = {'status': 'PARTIAL', 'error_type': type(exc).__name__}
                    meta['stores'][sid] = result
                    job = db.get(SyncJob, job_id)
                    job.metadata_json = deepcopy(meta)
                    db.commit()
                if result.get('ad_ready'):
                    ready.append(db.get(Store, sid))
                    dates[sid] = target
                if result.get('intraday_ready'):
                    intraday_snapshots[sid] = result['observation']['snapshot_id']
                if result.get('morning_ready'):
                    morning_dates[sid] = target.isoformat()
            # Night findings have already passed analysis. Release them after
            # the activity refresh, independently of new evidence/model calls.
            phase = 'overnight_digest'
            if send_alerts:
                try:
                    overnight_ids = queue_overnight_digest(db, settings, now or _utcnow())
                except Exception as exc:
                    db.rollback()
                    meta['overnight_digest_error'] = type(exc).__name__
                else:
                    meta.pop('overnight_digest_error', None)
                    meta['outbox_ids'] = list(dict.fromkeys([*meta.get('outbox_ids', []), *overnight_ids]))
            phase = 'morning_report'
            freeze_morning()
            phase = 'advertising_evidence'
            from jx_alert.ai.intraday_risk_evidence import intraday_risk_candidates
            reviewed_at = now or _utcnow()
            skills = load_ad_review_skills(settings)
            meta['ad_review_skills_status'] = skills.status
            contexts = [build_ad_review_context(db, settings, ready, dates, now=reviewed_at, skills=skills)]
            contexts[0]['campaigns'] = [r for r in contexts[0]['campaigns'] if r['current_state'] == '投放中']
            mature_history = {campaign_identity(r) for r in contexts[0]['campaigns']}
            intraday = []
            for sid, snapshot_id in intraday_snapshots.items():
                snapshot = db.get(RawSnapshot, snapshot_id)
                if snapshot:
                    intraday.extend(intraday_risk_candidates(
                        db, db.get(Store, sid), snapshot, now=reviewed_at, skills=skills))
            contexts.append({'ad_review': True, 'hourly_intraday': True, 'reply_mode': 'answer',
                'campaigns': intraday, 'policy': {'advertising_changes_allowed': False}})
            findings, model_usage, model_failures = [], [], []
            history = load_incident_history(db, reviewed_at)
            candidates = {}
            for context in contexts:
                period = 'intraday' if context.get('hourly_intraday') else 'mature'
                counts = {'supported': 0, 'deduplicated': 0, 'submitted': 0, 'confirmed': 0}
                context['hourly_delivery'] = True
                for row in context['campaigns']:
                    identity = campaign_identity(row)
                    signals = automatic_sp_signals(row, has_mature_history=identity in mature_history)
                    row['material_signals'] = signals
                    if not signals:
                        continue
                    counts['supported'] += 1
                    if (not repeat_eligible_signals(row, signals, history, reviewed_at)
                            or incident_already_recorded(db, row, now=reviewed_at)):
                        # Count notification reservations without withholding
                        # evidence from this hour's independent model review.
                        counts['deduplicated'] += 1
                candidates[period] = counts
            meta['review_counts'] = candidates
            db.rollback()
            phase = 'advertising_review'
            for context in contexts:
                period = 'intraday' if context.get('hourly_intraday') else 'mature'
                counts = candidates[period]
                if counts['supported']:
                    counts['submitted'] = counts['supported']
                    try:
                        found, audit = review_advertising((analyst_factory or DeepSeekClient.from_settings)(settings),
                            context, '逐活动复核当前实质广告风险。重复提醒由系统处理。')
                    except DeepSeekError:
                        model_failures.append(period)
                        continue
                    counts['confirmed'] = len(found)
                    findings.extend(found)
                    model_usage.append(audit)
            # Models do not grant a delivery right. Recheck fresh active master data.
            phase = 'evidence_recheck'
            rechecked_at = now or _utcnow()
            refreshed_skills = load_ad_review_skills(settings)
            refreshed = build_ad_review_context(db, settings, ready, dates, now=rechecked_at,
                skills=refreshed_skills)
            active = [r for r in refreshed['campaigns'] if r['current_state'] == '投放中']
            refreshed_history = {campaign_identity(r) for r in active}
            rechecked = [(r, automatic_sp_signals(r)) for r in active]
            for sid, snapshot_id in intraday_snapshots.items():
                snapshot = db.get(RawSnapshot, snapshot_id)
                if snapshot:
                    rechecked.extend((r, automatic_sp_signals(
                        r, has_mature_history=campaign_identity(r) in refreshed_history))
                        for r in intraday_risk_candidates(
                            db, db.get(Store, sid), snapshot, now=rechecked_at, skills=refreshed_skills))
            # IDs bind collected facts, but some automatic eligibility gates
            # (including a verified mature history for intraday) are external
            # to the ID. Check exact selected codes against fresh policy too.
            valid = {}
            for row, signals in rechecked:
                key = row['evidence_id']
                if key in valid:
                    valid[key] = None
                else:
                    valid[key] = (campaign_identity(row), frozenset(signals))
            checked = []
            for finding in findings:
                r = finding['evidence']
                supported = valid.get(r['evidence_id'])
                if (not supported or supported[0] != campaign_identity(r)
                        or not set(finding['signals']) <= supported[1]):
                    continue
                campaign = db.scalar(select(Campaign).where(Campaign.store_id == r['store_id'],
                    Campaign.campaign_type == r['ad_type'], Campaign.campaign_id == r['campaign_id']))
                store = db.get(Store, r['store_id'])
                if store and store.status == 1 and campaign and campaign.state == 'enabled':
                    checked.append(finding)
            phase = 'notification_queue'
            delivery_clock = now or _utcnow()
            ids = queue_hourly_findings(db, settings, checked, now=delivery_clock, slot=slot, send_alerts=send_alerts)
            meta['outbox_ids'] = list(dict.fromkeys([*meta.get('outbox_ids', []), *ids]))
            for field in ('error_type', 'error_phase', 'database_error_code'):
                meta.pop(field, None)
            phase = 'raw_archive'
            meta.update(finding_count=len(checked), model_usage=model_usage, model_failures=model_failures,
                raw_archive=archive_raw_snapshots(db, settings.raw_dir, after_id=meta['raw_start']))
            partial = (bool(model_failures) or 'overnight_digest_error' in meta or len(checked) != len(findings)
                       or any(r.get('status') == 'PARTIAL' for r in meta['stores'].values()))
            job = db.get(SyncJob, job_id)
            job.status = 'PARTIAL_FAILED' if partial else 'SUCCEEDED'
            finished_at = now or _utcnow()
            meta['retry_at'] = (finished_at + timedelta(minutes=5)).isoformat() if partial else None
        except Exception as exc:
            db.rollback()
            job = db.get(SyncJob, job_id)
            job.status = 'PARTIAL_FAILED'
            finished_at = now or _utcnow()
            meta.pop('database_error_code', None)
            if isinstance(exc, DBAPIError):
                args = getattr(exc.orig, 'args', ())
                if args and type(args[0]) is int:
                    meta['database_error_code'] = args[0]
            meta.update(error_type=type(exc).__name__, error_phase=phase,
                        retry_at=(finished_at + timedelta(minutes=5)).isoformat())
        job.finished_at, job.metadata_json = _naive(finished_at), deepcopy(meta)
        db.commit()
        return {'status': job.status, 'job_id': job_id, 'store_count': len(meta['stores']),
                'finding_count': meta.get('finding_count', 0), 'outbox_ids': meta.get('outbox_ids', [])}
