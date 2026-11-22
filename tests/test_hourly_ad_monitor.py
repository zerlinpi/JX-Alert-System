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
from jx_alert.notifications.ad_repeat import incident_key
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock
from jx_alert.storage.models import Campaign, Store, SyncJob


CLOCK = datetime(2026, 11, 18, 6, tzinfo=timezone.utc)
real_collect_store = monitor._collect_store


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


def test_hourly_rechecks_active_campaigns_without_repeating_delivered_alerts(warehouse):
    Analyst.calls = []
    source = Source()
    first = run(warehouse, source=source)
    assert first['status'] == 'SUCCEEDED' and first['finding_count'] == 1
    assert warehouse[3] == [('5174', '2026-11-16', False)]
    assert run(warehouse, now=CLOCK + timedelta(minutes=20), source=source)['status'] == 'ALREADY_COMPLETE'
    assert source.calls == 1
    with warehouse[1]() as db:
        message = db.get(NotificationOutbox, first['outbox_ids'][0])
        message.status = 'DELIVERED'
        message.payload = {**message.payload, 'hourly_delivered_items': message.payload['hourly_items']}
        db.commit()
    second = run(warehouse, now=CLOCK + timedelta(hours=1), source=source)
    assert second['status'] == 'SUCCEEDED' and second['finding_count'] == 1
    assert second['outbox_ids'] == [] and len(Analyst.calls) == 2
    with warehouse[1]() as db:
        rows = list(db.scalars(select(NotificationOutbox)))
        assert len(rows) == 1
        text = json.dumps(rows[0].payload, ensure_ascii=False)
        assert all(t not in text for t in ('未授权', '待补齐', '数据不足', 'UTC'))
        assert '成熟花费 2026-11-09' in text
        assert rows[0].payload['hourly_items'][0]['campaign_id'] == 'active'
        assert any(element['tag'] == 'table' for element in rows[0].payload['card']['body']['elements'])
        jobs = db.scalars(select(SyncJob).where(SyncJob.job_type == 'hourly_ad_monitor')).all()
        assert all('5174' in j.metadata_json['stores'] for j in jobs)
        assert db.get(SyncJob, second['job_id']).metadata_json['review_counts']['mature'] == {
            'supported': 1, 'deduplicated': 1, 'submitted': 1, 'confirmed': 1,
        }


def test_hourly_rechecks_exact_signal_not_just_evidence_id(warehouse, monkeypatch):
    original = monitor.build_ad_review_context
    passes = 0

    def revised_signals(*args, **kwargs):
        nonlocal passes
        passes += 1
        context = original(*args, **kwargs)
        if passes == 2:
            # This is the same fact ID and campaign, but the model-selected
            # signal is no longer supported by the fresh delivery policy.
            for row in context['campaigns']:
                row['material_signals'] = ['CPC']
        return context

    monkeypatch.setattr(monitor, 'build_ad_review_context', revised_signals)
    result = run(warehouse)
    assert result['status'] == 'PARTIAL_FAILED'
    assert not result['outbox_ids'] and result['finding_count'] == 0
    with warehouse[1]() as db:
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


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
    assert incident_key(row) == incident_key({**row, 'material_signals': ['CPA', 'ROAS']})
    assert incident_key(row) != incident_key({**row, 'intraday': True})


def _finding(sid, cid, day='2026-11-16'):
    return {'evidence': {'store_id': sid, 'campaign_id': cid, 'ad_type': 'sp', 'store_name': '店铺' + sid,
        'campaign_name': cid, 'report_date': day, 'currency': 'USD', 'kpis': {},
        'mature_date': (datetime.fromisoformat(day) - timedelta(days=7)).date().isoformat(),
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
        initial = json.dumps(hourly_ads.recheck_hourly_delivery(db, settings, row.payload, CLOCK)[0])
        assert initial.count('<at id=ou_owner></at>') == 1 and 'ou_nonmember' not in initial
        private = hourly_ads.recheck_hourly_delivery(db, settings, lead.payload, CLOCK)
        assert private is not None and '<at id' not in json.dumps(private)
        routing['stores']['5175'] = ['ou_new_owner']
        path.write_text(json.dumps(routing))
        refreshed, included = hourly_ads.recheck_hourly_delivery(db, settings, row.payload, CLOCK)
        assert '<at id=ou_new_owner></at>' in json.dumps(refreshed)
        table = next(item for item in refreshed['body']['elements'] if item['tag'] == 'table')
        assert len(table['rows']) == 2
        assert included == row.payload['hourly_items']
        previous_version = {**lead.payload, 'delivery': {'role': 'store_ad_recipient',
            'receive_id_type': 'open_id', 'receive_id': 'ou_owner'}}
        assert hourly_ads.recheck_hourly_delivery(db, settings, previous_version, CLOCK) is None
        routing['ad_alert_subscribers']['recipients'][0]['generation'] = 'b'*32
        path.write_text(json.dumps(routing))
        assert hourly_ads.recheck_hourly_delivery(db, settings, lead.payload, CLOCK) is None
        changed_date = [_finding('5174', 'active', day='2026-11-17')]
        assert not hourly_ads.queue_hourly_findings(db, settings, changed_date,
            now=CLOCK + timedelta(hours=1), slot='second', send_alerts=True)


def test_delivered_group_records_only_visible_activities_and_later_rechecks_missing_one(warehouse, monkeypatch):
    from jx_alert.notifications import worker
    from jx_alert.notifications.feishu_app import FeishuAppClient

    settings, sessions, _, _ = warehouse
    clock = CLOCK
    sends = []

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

    class Feishu:
        def send_card(self, card, **kwargs):
            sends.append(card)
            return {'message_id': 'om_receipt'}

    monkeypatch.setattr(worker, 'datetime', FixedClock)
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    monkeypatch.setattr(FeishuAppClient, 'from_settings', lambda _: Feishu())
    findings = [_finding('5174', 'active'), _finding('5174', 'other')]
    with sessions() as db:
        other = Campaign(store_id='5174', campaign_id='other', campaign_type='sp', state='enabled',
            updated_at=clock.replace(tzinfo=None))
        db.add(other)
        db.commit()
        first, = hourly_ads.queue_hourly_findings(db, settings, findings, now=clock, slot='first', send_alerts=True)
        # The queued card had both activities. An activity disabled at actual
        # send time must be absent from the committed delivery receipt.
        row = db.get(NotificationOutbox, first)
        row.next_attempt_at = row.created_at = clock.replace(tzinfo=None)
        other.state = 'paused'
        db.commit()
    assert worker.process_outbox_batch(outbox_ids=[first])['sent'] == 1
    with sessions() as db:
        row = db.get(NotificationOutbox, first)
        assert row.status == 'DELIVERED'
        assert [i['campaign_id'] for i in row.payload['hourly_items']] == ['active', 'other']
        assert [i['campaign_id'] for i in row.payload['hourly_delivered_items']] == ['active']
        assert row.payload['card'] == sends[0]
        other = db.scalar(select(Campaign).where(Campaign.campaign_id == 'other'))
        clock += timedelta(hours=1)
        other.state = 'enabled'
        other.updated_at = clock.replace(tzinfo=None)
        db.commit()
        replacement, = hourly_ads.queue_hourly_findings(
            db, settings, findings, now=clock, slot='second', send_alerts=True)
        assert [i['campaign_id'] for i in db.get(NotificationOutbox, replacement).payload['hourly_items']] == ['other']


def test_group_retry_does_not_repeat_a_delivered_lead_copy(warehouse):
    settings, sessions, _, _ = warehouse
    path = Path(settings.feishu_routing_file)
    routing = json.loads(path.read_text())
    routing['ad_alert_subscribers'] = {'recipients': [
        {'open_id': 'ou_lead', 'name': '总负责人', 'generation': 'a'*32},
    ]}
    path.write_text(json.dumps(routing))
    findings = [_finding('5174', 'active')]
    with sessions() as db:
        group_id, lead_id = hourly_ads.queue_hourly_findings(
            db, settings, findings, now=CLOCK, slot='first', send_alerts=True)
        db.get(NotificationOutbox, group_id).status = 'DEAD'
        lead = db.get(NotificationOutbox, lead_id)
        lead.status = 'DELIVERED'
        lead.payload = {**lead.payload, 'hourly_delivered_items': lead.payload['hourly_items']}
        db.commit()
        new_id, = hourly_ads.queue_hourly_findings(
            db, settings, findings, now=CLOCK + timedelta(hours=1), slot='second', send_alerts=True)
        assert db.get(NotificationOutbox, new_id).chat_id == 'oc_reports'


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
        campaign.updated_at = (open_at - timedelta(minutes=10)).replace(tzinfo=None)
        db.commit()
        assert not hourly_ads.queue_overnight_digest(db, settings, open_at)  # before morning refresh
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


def test_budget_alert_explains_the_actual_spend_and_difference():
    item = hourly_ads.alert_item({'signals': ['intraday_budget'], 'evidence': {
        'store_id': '5174', 'ad_type': 'sp', 'campaign_id': '42', 'store_name': '测试店铺',
        'campaign_name': '测试活动', 'intraday': True, 'currency': 'USD',
        'current': {'spend': 50.08}, 'verified_daily_budget': 1,
    }})
    assert item['signal'] == '今天的花费远超平均日预算'
    assert item['numbers'].splitlines() == ['今天已花 美元 50.08', '当前日预算 美元 1.00', '比当前日预算多 美元 49.08']


def test_group_card_gives_verified_click_impact_and_specific_first_check():
    finding = _finding('5174', 'AirBird活动', day='2026-11-17')
    finding['signals'] = ['CTR']
    finding['evidence'].update(current={'spend': 30.87, 'clicks': 28, 'impressions': 4046},
        kpis={'CTR': {'current': 28 / 4046, 'baseline': .0121833693, 'comparison_basis': 'SEVEN_DAY'}})
    item = hourly_ads.alert_item(finding, include_dates=True)
    value = hourly_ads.advertising_card([item])
    row, = next(x['rows'] for x in value['body']['elements'] if x['tag'] == 'table')
    assert '先查 搜索词与广告位' in row['c1']
    assert '2026-11-17 美元 30.87' in row['c2']
    assert '点击率 0.69%（前七日 1.22%，低 0.53 个百分点）' in row['c2']
    assert '按参考点击率估算少约 21 次点击' in row['c2']


def test_zero_conversion_card_separates_attributed_orders_from_reference_estimate():
    finding = _finding('5174', '零转化活动')
    finding['evidence']['mature_comparison'] = {'expected_orders_at_reference_rate': 4.5}
    item = hourly_ads.alert_item(finding, include_dates=True)
    assert '归因订单 0' in item['numbers']
    assert '按参考转化率估算 4.5 单' in item['numbers']
    assert '花费最多的投放与商品页' in item['next_check']


def test_same_campaign_groups_current_budget_risk_before_mature_finding(warehouse):
    settings, sessions, _, _ = warehouse
    daily = _finding('5174', 'active')
    budget = {'signals': ['intraday_budget'], 'evidence': {**daily['evidence'],
        'intraday': True, 'current': {'spend': 50.08}, 'verified_daily_budget': 1.0}}
    with sessions() as db:
        row_id, = hourly_ads.queue_hourly_findings(db, settings, [daily, budget],
            now=CLOCK, slot='paired-risks', send_alerts=True)
        item, = db.get(NotificationOutbox, row_id).payload['hourly_items']
        assert item['signal'] == '观测日的花费远超平均日预算'
        assert '比当前日预算多 美元 49.08' in item['numbers']


def test_single_day_no_clicks_has_independent_evidence_and_table(warehouse):
    settings, sessions, _, _ = warehouse
    evidence = {**_finding('5174', 'active')['evidence'], 'campaign_start_date': '2026-10-01',
        'intraday': True, 'baseline': {}, 'mature': {}, 'material_signals': ['intraday_no_clicks'],
        'current': {'spend': 0, 'clicks': 0, 'impressions': 10000}}
    signals = monitor.automatic_sp_signals(evidence)
    assert signals == ['intraday_no_clicks']
    assert monitor.automatic_sp_signals({**evidence, 'campaign_start_date': '2026-11-14'}) == []
    assert monitor.automatic_sp_signals({**evidence, 'current': {**evidence['current'], 'impressions': 9999}}) == []
    with sessions() as db:
        row_id, = hourly_ads.queue_hourly_findings(db, settings, [{'signals': signals, 'evidence': evidence}],
            now=CLOCK, slot='same-day-no-clicks', send_alerts=True)
        payload = db.get(NotificationOutbox, row_id).payload
        table, = [x for x in payload['card']['body']['elements'] if x['tag'] == 'table']
        row, = table['rows']
        assert '观测日有大量曝光，点击仍为零' in row['c1']
        assert '当日曝光 10,000' in row['c2'] and '当日点击 0' in row['c2']
        assert '订单' not in row['c2'] and '参考' not in row['c2']


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
    def offline_digest(*args, **kwargs):
        raise RuntimeError('overnight outbox unavailable')

    monkeypatch.setattr(monitor, 'queue_overnight_digest', offline_digest)
    assert run(warehouse, now=morning)['status'] == 'PARTIAL_FAILED'
    assert len(captured) == 1 and captured[0]['prepare'] is True
    assert captured[0]['store_dates'] == {'5174': '2026-11-16'}
    assert operations.queue_scheduled_daily_reports(now=send_at - timedelta(seconds=1))['outbox_ids'] == []
    assert operations.queue_scheduled_daily_reports(now=send_at)['outbox_ids'] == [123]
    assert len(captured) == 1


def test_morning_sales_coverage_is_dated_and_requires_store_and_both_sku_grains(warehouse):
    from jx_alert.storage.coverage import mark_coverage

    day = CLOCK.date() - timedelta(days=1)
    with warehouse[1]() as db:
        mark_coverage(db, store_id='5174', metric_date=day, dimension='campaign_sp', row_count=3)
        db.flush()
        assert monitor._morning_sales_status(db, '5174', day)['store_sales'] == 'NOT_COLLECTED'
        mark_coverage(db, store_id='5174', metric_date=day, dimension='store', row_count=0)
        db.flush()
        assert not monitor._morning_sales_status(db, '5174', day)['ready']
        for dim in ('sku_asin', 'sku_msku'):
            mark_coverage(db, store_id='5174', metric_date=day, dimension=dim, row_count=0)
        db.flush()
        status = monitor._morning_sales_status(db, '5174', day)
        assert status['ready'] and status['store_sales'] == 'EMPTY'
        assert not monitor._morning_sales_status(db, '5174', day + timedelta(days=1))['ready']


def test_morning_sales_source_failure_does_not_skip_other_date_or_hide_behind_ads(
        warehouse, monkeypatch):
    from jx_alert.lingxing import api_business, business_dashboard, campaign_collector, collector
    from jx_alert.lingxing import intraday_observation, sales_collector
    from jx_alert.reporting import archive
    from jx_alert.storage.coverage import mark_coverage

    settings, factory, _, _ = warehouse
    settings.lingxing_performance_enabled = False
    day = CLOCK.date() - timedelta(days=2)
    tried = []

    def get_sales(client, db, dates, **kwargs):
        from datetime import date
        actual = date.fromisoformat(dates['5174'])
        tried.append(actual)
        if actual == day:
            raise RuntimeError('upstream failure')
        for dim in ('store', 'sku_asin', 'sku_msku'):
            mark_coverage(db, store_id='5174', metric_date=actual, dimension=dim, row_count=0)
        db.commit()

    monkeypatch.setattr(sales_collector, 'collect_sales_for_day', get_sales)
    monkeypatch.setattr(api_business, 'collect_api_business_for_day', lambda *a: {'status': 'COMPLETE'})
    monkeypatch.setattr(business_dashboard, 'collect_business_for_day', lambda *a: {'status': 'COMPLETE'})
    monkeypatch.setattr(campaign_collector, 'collect_campaigns', lambda *a, **kw: None)
    monkeypatch.setattr(collector, 'collect_ads_for_day', lambda *a, **kw: {'rows': {'sp': 3}})
    monkeypatch.setattr(intraday_observation, 'run_observation', lambda *a, **kw: {'status': 'COMPLETE'})
    monkeypatch.setattr(archive, 'archive_partitions', lambda *a: {})
    client = type('Client', (), {'seller_ad_accounts': lambda self: {'data': []}})()
    # Restore the collector overridden by the warehouse fixture.
    with factory() as db:
        status = real_collect_store(client, db, settings, Source().get_stores()['data'][0],
                                    day, morning=True, now=CLOCK)
    assert tried == [day, day - timedelta(days=1)]
    assert status['ad_ready'] and status['morning_ready'] and status['status'] == 'PARTIAL'
    assert status['sales_coverage'][day.isoformat()] == {'ready': False, 'collection': 'FAILED'}
    assert status['sales_coverage'][(day - timedelta(days=1)).isoformat()]['store_sales'] == 'EMPTY'


def test_existing_night_summary_does_not_wait_for_new_ad_analysis_and_survives_retry(warehouse, monkeypatch):
    collect = monitor._collect_store
    evidence = monitor.build_ad_review_context
    morning = CLOCK.replace(hour=0)
    events = []

    def collect_store(*args, **kwargs):
        events.append('refresh')
        return collect(*args, **kwargs)

    def release(*args, **kwargs):
        events.append('digest')
        return [123] if events.count('digest') == 1 else []

    def fail(*args, **kwargs):
        events.append('analysis')
        raise RuntimeError('new analysis unavailable')

    monkeypatch.setattr(monitor, '_collect_store', collect_store)
    monkeypatch.setattr(monitor, 'queue_overnight_digest', release)
    monkeypatch.setattr(monitor, 'build_ad_review_context', fail)
    first = run(warehouse, now=morning)
    assert events == ['refresh', 'digest', 'analysis']
    assert first['status'] == 'PARTIAL_FAILED' and first['outbox_ids'] == [123]
    monkeypatch.setattr(monitor, 'build_ad_review_context', evidence)
    with warehouse[1]() as db:
        job = db.get(SyncJob, first['job_id'])
        assert job.metadata_json['error_phase'] == 'advertising_evidence'
    recovered = run(warehouse, now=morning + timedelta(minutes=5))
    assert recovered['status'] == 'SUCCEEDED' and recovered['outbox_ids'][0] == 123


def test_hourly_failed_sp_attempt_preserves_daily_coverage_without_reporting_success(warehouse, monkeypatch):
    from jx_alert.lingxing import campaign_collector, intraday_observation
    from jx_alert.lingxing.collector import collect_ads_for_day
    from jx_alert.storage.models import DataCoverage

    class API:
        def seller_ad_accounts(self):
            return {"data": []}

        def iter_advertising_report(self, kind, payload):
            return iter([{"campaign_id": "active", "report_date": payload["report_date"],
                          "cost": 17, "orders": 2, "sales": 50, "units": 2,
                          "clicks": 12, "impressions": 1200}])

    class FailedAPI(API):
        def iter_advertising_report(self, kind, payload):
            raise RuntimeError("Simulated SP provider failure")

    settings, factory, _, _ = warehouse
    target = (CLOCK - timedelta(days=2)).date()
    record = Source().get_stores()["data"][0]
    monkeypatch.setattr(campaign_collector, "collect_campaigns", lambda *a, **kw: None)
    monkeypatch.setattr(intraday_observation, "run_observation", lambda *a, **kw: {"status": "COMPLETE"})
    with factory() as db:
        collect_ads_for_day(API(), db, {"5174": target.isoformat()},
                            stores_response={"data": [record]}, report_types=("sp",))
        query = select(DataCoverage).where(DataCoverage.dimension == "campaign_sp",
                                          DataCoverage.metric_date == target)
        prior = db.scalar(query)
        expected = (prior.status, prior.row_count, prior.completed_at)

    with factory() as db:
        status = real_collect_store(FailedAPI(), db, settings, record, target, now=CLOCK)
        assert status["status"] == "PARTIAL" and not status["ad_ready"]
        assert status["reports"][target.isoformat()] == {"status": "FAILED"}
        coverage = db.scalar(query)
        assert (coverage.status, coverage.row_count, coverage.completed_at) == expected


def test_hourly_card_preserves_activity_date_basis_and_maturity():
    finding = _finding('5174', '42', day='2026-11-18')
    finding['evidence'].update(source='lingxing_ads_sp', attribution_days=7)
    item = hourly_ads.alert_item(finding, include_dates=True)
    rendered = hourly_ads.advertising_card([item])
    table, = [e for e in rendered['body']['elements'] if e['tag'] == 'table']
    row, = table['rows']
    assert 'SP · 活动 42' in row['c0']
    assert '转化成熟日 · 店铺当地 2026-11-11 · 7 天归因' in row['c2']
    assert '来源：领星广告日报' in row['c2']
    assert '估算' not in row['c2']
    assert '1 家店铺 · 1 个活动待复核' in json.dumps(rendered, ensure_ascii=False)


@pytest.mark.parametrize('minutes,day,budget,valid', [
    (20, '2026-11-17', 25, True), (76, '2026-11-17', 25, False),
    (20, '2026-11-16', 25, False), (20, '2026-11-17', 20, False),
])
def test_intraday_evidence_window_and_budget_are_rechecked(minutes, day, budget, valid):
    observed = (CLOCK - timedelta(minutes=minutes)).isoformat()
    item = {'live_observation': {'day': day, 'captured_at': observed, 'daily_budget': budget}}
    from types import SimpleNamespace

    store = SimpleNamespace(timezone='America/Los_Angeles')
    campaign = SimpleNamespace(budget=25)
    assert hourly_ads._live_observation_current(item, campaign, store, CLOCK) is valid
