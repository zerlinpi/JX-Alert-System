"""Hourly updates are bounded, quiet, active-only and idempotent across polls."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from jx_alert import hourly_ad_monitor as monitor
from jx_alert.config import Settings
from jx_alert.notifications import hourly_ads
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock
from jx_alert.storage.models import Campaign, Store, SyncJob


CLOCK = datetime(2026, 11, 18, 6, tzinfo=timezone.utc)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'data.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    scope, zones, routing = (tmp_path / name for name in ('scope.json', 'zones.json', 'routing.json'))
    scope.write_text(json.dumps({'store_ids': [5174, 5175]}))
    zones.write_text(json.dumps({'stores': {sid: {'timezone': 'America/Los_Angeles', 'currency_code': 'USD'}
                                          for sid in ('5174', '5175')}}))
    routing.write_text(json.dumps({'groups': {'reports': {'chat_id': 'oc_reports'}}, 'default_chat_id': 'reports'}))
    settings = Settings(_env_file=None, app_dry_run=False, scheduler_enabled=True,
        hourly_ad_monitor_enabled=True, feishu_enabled=True, feishu_transport='app',
        feishu_group_fallback_enabled=True, feishu_daily_report_chat_id='reports',
        feishu_routing_file=str(routing), collection_scope_file=str(scope), store_timezones_file=str(zones),
        store_controls_file=str(tmp_path / 'controls.json'), raw_dir=str(tmp_path / 'raw'))
    with factory() as db:
        db.add_all([Store(sid=sid, name='店铺' + sid, status=1 if sid == '5174' else 2,
            timezone='America/Los_Angeles', currency_code='USD', has_ads_setting=1) for sid in ('5174', '5175')])
        db.add_all([Campaign(store_id='5174', campaign_id=key, campaign_type='sp', name=key,
            state=state, updated_at=CLOCK.replace(tzinfo=None)) for key, state in (('active', 'enabled'), ('paused', 'paused'))])
        db.commit()
    calls = []
    def collected(client, db, settings, record, target, **kwargs):
        calls.append((str(record['sid']), target.isoformat(), kwargs['morning']))
        return {'status': 'COMPLETE', 'ad_ready': True, 'intraday_ready': False}
    monkeypatch.setattr(monitor, '_collect_store', collected)
    def evidence(db, settings, stores, dates, **kwargs):
        campaigns = []
        for store in stores:
            for c in db.scalars(select(Campaign).where(Campaign.store_id == store.sid)):
                campaigns.append({'evidence_id': c.campaign_id, 'store_id': store.sid, 'store_name': store.name,
                    'campaign_id': c.campaign_id, 'campaign_name': c.name, 'ad_type': 'sp',
                    'current_state': '投放中' if c.state == 'enabled' else '已暂停',
                    'report_date': '2026-11-16', 'mature_date': '2026-11-09', 'currency': 'USD',
                    'current': {'spend': 30, 'clicks': 20, 'impressions': 1000},
                    'mature': {'spend': 70, 'orders': 0, 'clicks': 40}, 'kpis': {},
                    'material_signals': ['zero_conversion']})
        return {'ad_review': True, 'campaigns': campaigns}
    monkeypatch.setattr(monitor, 'build_ad_review_context', evidence)
    yield settings, factory, engine, calls
    engine.dispose()


class Source:
    calls = 0
    def get_stores(self):
        self.calls += 1
        return {'data': [{'sid': sid, 'name': '店铺' + sid, 'status': status, 'has_ads_setting': 1}
                         for sid, status in (('5174', 1), ('5175', 2))]}


class Analyst:
    calls = []
    def analyze(self, context, question):
        self.calls.append(context)
        assert {r['campaign_name'] for r in context['campaigns']} == {'active'}
        assert context['hourly_delivery'] is True
        return json.dumps({'findings': [{'evidence_id': 'active', 'signals': ['zero_conversion']}]})


def run(warehouse, *, now=CLOCK, source=None, analyst=None, **kwargs):
    settings, factory, engine, _ = warehouse
    return monitor.run_hourly_monitor(settings=settings, session_factory=factory, warehouse_engine=engine,
        now=now, client_factory=lambda: source or Source(), analyst_factory=lambda _: analyst or Analyst(), **kwargs)


def test_hourly_active_only_without_quality_pages_and_deduplicates_future_hour(warehouse):
    Analyst.calls = []
    source = Source()
    first = run(warehouse, source=source)
    assert first['status'] == 'SUCCEEDED' and first['finding_count'] == 1
    assert warehouse[3] == [('5174', '2026-11-16', False)]
    assert run(warehouse, now=CLOCK + timedelta(minutes=20), source=source)['status'] == 'ALREADY_COMPLETE'
    assert source.calls == 1
    second = run(warehouse, now=CLOCK + timedelta(hours=1), source=source)
    assert second['status'] == 'SUCCEEDED' and second['outbox_ids'] == []
    assert len(Analyst.calls) == 1
    with warehouse[1]() as db:
        rows = list(db.scalars(select(NotificationOutbox)))
        assert len(rows) == 1
        text = json.dumps(rows[0].payload, ensure_ascii=False)
        assert all(t not in text for t in ('未授权', '待补齐', '数据不足', '2026-11-09', 'UTC'))
        assert rows[0].payload['hourly_items'][0]['campaign_id'] == 'active'
        assert any(element['tag'] == 'table' for element in rows[0].payload['card']['body']['elements'])
        jobs = db.scalars(select(SyncJob).where(SyncJob.job_type == 'hourly_ad_monitor')).all()
        assert all('5174' in j.metadata_json['stores'] for j in jobs)


def test_source_failure_waits_before_retry_and_retains_job(warehouse):
    class Failed:
        def get_stores(self):
            raise RuntimeError('private upstream details must not be stored')
    first = run(warehouse, source=Failed(), send_alerts=False)
    assert first['status'] == 'PARTIAL_FAILED'
    assert run(warehouse, now=CLOCK + timedelta(minutes=4), send_alerts=False)['status'] == 'WAITING'
    assert run(warehouse, now=CLOCK + timedelta(minutes=5), send_alerts=False)['status'] == 'SUCCEEDED'
    with warehouse[1]() as db:
        job = db.get(SyncJob, first['job_id'])
        assert job.metadata_json['attempts'] == 2
        assert 'private upstream' not in json.dumps(job.metadata_json)
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_interrupted_final_attempt_resumes_after_lock_release(warehouse):
    _, factory, engine, calls = warehouse
    with factory() as db:
        job = SyncJob(job_type='hourly_ad_monitor_validation', status='RUNNING',
            started_at=CLOCK.replace(tzinfo=None),
            metadata_json={'slot': CLOCK.isoformat(), 'attempts': 3, 'stores': {}})
        db.add(job)
        db.commit()
        job_id = job.id

    with database_job_lock(engine, 'hourly_ad_monitor'):
        with pytest.raises(JobAlreadyRunning):
            run(warehouse, send_alerts=False)
    assert calls == []

    result = run(warehouse, send_alerts=False)
    assert result['status'] == 'SUCCEEDED'
    assert result['job_id'] == job_id
    with factory() as db:
        job = db.get(SyncJob, job_id)
        assert job.metadata_json['attempts'] == 3
        assert job.finished_at == CLOCK.replace(tzinfo=None)


def test_operational_failure_cooldown_starts_at_completion(warehouse, monkeypatch):
    clock = CLOCK
    monkeypatch.setattr(monitor, '_utcnow', lambda: clock)
    completion = CLOCK + timedelta(minutes=8)

    class SlowFailure:
        def get_stores(self):
            nonlocal clock
            clock = completion
            raise OperationalError('private statement', None, RuntimeError(1038, 'private details'))

    result = run(warehouse, now=None, source=SlowFailure(), send_alerts=False)
    assert result['status'] == 'PARTIAL_FAILED'
    retry_at = completion + timedelta(minutes=5)
    with warehouse[1]() as db:
        job = db.get(SyncJob, result['job_id'])
        assert job.finished_at == completion.replace(tzinfo=None)
        assert job.metadata_json['retry_at'] == retry_at.isoformat()
        assert job.metadata_json['error_phase'] == 'inventory'
        assert job.metadata_json['database_error_code'] == 1038
        assert 'private' not in json.dumps(job.metadata_json)

    clock = retry_at - timedelta(seconds=1)
    source = Source()
    assert run(warehouse, now=None, source=source, send_alerts=False)['status'] == 'WAITING'
    assert source.calls == 0
    clock = retry_at
    resumed = run(warehouse, now=None, source=source, send_alerts=False)
    assert resumed['status'] == 'SUCCEEDED'
    assert resumed['job_id'] == result['job_id']
    assert source.calls == 1


def test_activity_paused_during_model_call_never_queues(warehouse):
    class Pausing(Analyst):
        def analyze(self, context, question):
            answer = super().analyze(context, question)
            with warehouse[1]() as db:
                c = db.scalar(select(Campaign).where(Campaign.campaign_id == 'active'))
                c.state = 'paused'
                db.commit()
            return answer
    assert run(warehouse, analyst=Pausing())['outbox_ids'] == []
    with warehouse[1]() as db:
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_changed_metric_selection_does_not_create_a_second_incident():
    row = {'store_id': '5174', 'campaign_id': '1', 'ad_type': 'sp', 'report_date': '2026-11-16', 'mature_date': '2026-11-09'}
    assert hourly_ads.incident_key(row) == hourly_ads.incident_key({**row, 'material_signals': ['CPA', 'ROAS']})
    assert hourly_ads.incident_key(row) != hourly_ads.incident_key({**row, 'intraday': True})


def _finding(sid, cid, day='2026-11-16'):
    return {'evidence': {'store_id': sid, 'campaign_id': cid, 'ad_type': 'sp', 'store_name': '店铺' + sid,
        'campaign_name': cid, 'report_date': day, 'currency': 'USD', 'kpis': {},
        'current': {'spend': 50, 'clicks': 27}, 'mature': {'spend': 120, 'clicks': 50, 'orders': 0}},
        'signals': ['zero_conversion']}


def test_daytime_batches_in_one_group_and_refreshes_owner_mentions(warehouse):
    settings, session, _, _ = warehouse
    settings.feishu_group_fallback_enabled = False
    path = Path(settings.feishu_routing_file)
    routing = json.loads(path.read_text())
    routing['groups']['reports']['verified_member_open_ids'] = ['ou_owner', 'ou_new_owner', 'ou_lead']
    routing['stores'] = {'5174': {'open_ids': ['ou_owner']}, '5175': ['ou_owner', 'ou_nonmember']}
    routing['ad_alert_subscribers'] = {'recipients': [
        {'open_id': 'ou_lead', 'name': '总负责人', 'generation': 'a'*32},
    ]}
    path.write_text(json.dumps(routing))
    with session() as db:
        db.get(Store, '5175').status = 1
        db.add(Campaign(store_id='5175', campaign_id='b', campaign_type='sp', state='enabled',
                        updated_at=CLOCK.replace(tzinfo=None)))
        db.commit()
        evidence = [_finding('5174', 'active'), _finding('5175', 'b'), _finding('5174', 'paused')]
        ids = hourly_ads.queue_hourly_findings(db, settings, evidence, now=CLOCK, slot='first', send_alerts=True)
        assert len(ids) == 2
        row, lead = (db.get(NotificationOutbox, oid) for oid in ids)
        assert (row.chat_id, lead.chat_id) == ('oc_reports', 'ou_lead')
        assert [item['campaign_id'] for item in row.payload['hourly_items']] == ['active', 'b']
        assert lead.payload['hourly_items'] == row.payload['hourly_items']
        initial = json.dumps(hourly_ads.recheck_hourly_card(db, settings, row.payload, CLOCK))
        assert initial.count('<at id=ou_owner></at>') == 1 and 'ou_nonmember' not in initial
        private = hourly_ads.recheck_hourly_card(db, settings, lead.payload, CLOCK)
        assert private is not None and '<at id' not in json.dumps(private)
        routing['stores']['5175'] = ['ou_new_owner']
        path.write_text(json.dumps(routing))
        refreshed = hourly_ads.recheck_hourly_card(db, settings, row.payload, CLOCK)
        assert '<at id=ou_new_owner></at>' in json.dumps(refreshed)
        table = next(item for item in refreshed['body']['elements'] if item['tag'] == 'table')
        assert len(table['rows']) == 2
        previous_version = {**lead.payload, 'delivery': {'role': 'store_ad_recipient',
            'receive_id_type': 'open_id', 'receive_id': 'ou_owner'}}
        assert hourly_ads.recheck_hourly_card(db, settings, previous_version, CLOCK) is None
        routing['ad_alert_subscribers']['recipients'][0]['generation'] = 'b'*32
        path.write_text(json.dumps(routing))
        assert hourly_ads.recheck_hourly_card(db, settings, lead.payload, CLOCK) is None
        changed_date = [_finding('5174', 'active', day='2026-11-17')]
        assert not hourly_ads.queue_hourly_findings(db, settings, changed_date,
            now=CLOCK + timedelta(hours=1), slot='second', send_alerts=True)


def test_night_keeps_incidents_and_releases_one_group_table_at_eight(warehouse):
    settings, session, _, _ = warehouse
    path = Path(settings.feishu_routing_file)
    routing = json.loads(path.read_text())
    routing['stores'] = {'5174': ['ou_owner']}
    routing['ad_alert_subscribers'] = {'recipients': [
        {'open_id': 'ou_lead', 'name': '总负责人', 'generation': 'a'*32},
    ]}
    path.write_text(json.dumps(routing))
    close = CLOCK + timedelta(hours=4)
    open_at = close + timedelta(hours=14)
    with session() as db:
        campaign = db.scalar(select(Campaign).where(Campaign.campaign_id == 'active'))
        campaign.updated_at = close.replace(tzinfo=None)
        db.commit()
        evidence = [_finding('5174', 'active')]
        assert not hourly_ads.queue_hourly_findings(db, settings, evidence, now=close, slot='night', send_alerts=True)
        assert db.scalar(select(SyncJob.status).where(SyncJob.job_type == hourly_ads.INCIDENT)) == 'HELD'
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert not hourly_ads.queue_overnight_digest(db, settings, open_at - timedelta(seconds=1))
        assert not hourly_ads.queue_overnight_digest(db, settings, open_at)  # old activity state
        campaign.updated_at = open_at.replace(tzinfo=None)
        db.commit()
        ids = hourly_ads.queue_overnight_digest(db, settings, open_at)
        assert len(ids) == 2
        row, lead = (db.get(NotificationOutbox, oid) for oid in ids)
        assert (row.chat_id, lead.chat_id) == ('oc_reports', 'ou_lead')
        assert '<at id=ou_owner></at>' in json.dumps(row.payload['card'])
        assert '<at id' not in json.dumps(lead.payload['card'])
        assert row.payload['text'] == '夜间广告异常汇总'
        assert hourly_ads.delivery_window(row.payload, open_at - timedelta(seconds=1))[0] == 'WAITING'
        assert hourly_ads.delivery_window(row.payload, open_at)[0] == 'READY'
        assert hourly_ads.delivery_window(row.payload, open_at + timedelta(hours=10))[0] == 'EXPIRED'
        assert not hourly_ads.queue_overnight_digest(db, settings, open_at + timedelta(minutes=1))


def test_legacy_multiple_leads_have_only_the_group_destination(warehouse):
    settings, session, _, _ = warehouse
    path = Path(settings.feishu_routing_file)
    routing = json.loads(path.read_text())
    routing['ad_alert_subscribers'] = {'recipients': [
        {'open_id': f'ou_lead{index}', 'name': '旧订阅人员', 'generation': 'a'*32}
        for index in range(2)
    ]}
    path.write_text(json.dumps(routing))
    with session() as db:
        ids = hourly_ads.queue_hourly_findings(db, settings, [_finding('5174', 'active')],
            now=CLOCK, slot='first', send_alerts=True)
        assert [db.get(NotificationOutbox, oid).chat_id for oid in ids] == ['oc_reports']


def test_automatic_analysis_waits_for_campaign_history_and_excludes_spend_alone():
    signals = monitor.automatic_sp_signals
    row = {'report_date': '2026-11-16', 'material_signals': ['spend', 'CPC']}
    assert signals({**row, 'campaign_start_date': '2026-11-11'}) == []
    assert signals({**row, 'campaign_start_date': '2026-11-09'}) == ['CPC']
    assert signals({**row, 'material_signals': ['spend']}) == []
    intraday = {**row, 'intraday': True, 'material_signals': ['intraday_budget'],
                'current': {'spend': 230}, 'verified_daily_budget': 100}
    assert not signals(intraday)
    almost = {**intraday, 'current': {'spend': 229.99}}
    assert not signals(almost, has_mature_history=True)
    assert signals(intraday, has_mature_history=True) == ['intraday_budget']


def test_nine_oclock_reuses_only_the_prepared_morning_report(warehouse, monkeypatch):
    from jx_alert import config, operations
    from jx_alert.notifications import daily_report
    from jx_alert.storage import db as storage

    settings, factory, _, _ = warehouse
    settings.feishu_daily_report_enabled = True
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    monkeypatch.setattr(storage, 'SessionLocal', factory)
    morning = CLOCK.replace(hour=0)
    send_at = morning + timedelta(hours=1)
    assert operations.queue_scheduled_daily_reports(now=send_at)['status'] == 'WAITING_MORNING_SNAPSHOT'
    captured = []
    def enqueue(db, settings, **kwargs):
        captured.append(kwargs)
        return {'queued': 1, 'outbox_ids': [123]}
    monkeypatch.setattr(daily_report, 'enqueue_daily_snapshots', enqueue)
    monkeypatch.setattr(monitor, '_collect_store', lambda *args, **kwargs:
        {'status': 'COMPLETE', 'morning_ready': True, 'ad_ready': False})
    run(warehouse, now=morning)
    assert len(captured) == 1 and captured[0]['prepare'] is True
    assert captured[0]['store_dates'] == {'5174': '2026-11-16'}
    assert operations.queue_scheduled_daily_reports(now=send_at - timedelta(seconds=1))['outbox_ids'] == []
    assert operations.queue_scheduled_daily_reports(now=send_at)['outbox_ids'] == [123]
    assert len(captured) == 1
