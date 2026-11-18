"""Hourly updates are bounded, quiet, active-only and idempotent across polls."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from jx_alert import hourly_ad_monitor as monitor
from jx_alert.config import Settings
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
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
        return json.dumps({'findings': [{'evidence_id': 'active', 'signals': ['zero_conversion'],
            'analysis': '成熟点击没有带来归因订单，需负责人复核流量质量。'}]})


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
        assert rows[0].payload['ad_campaign_id'] == 'active'
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
    assert monitor._incident_key(row) == monitor._incident_key({**row, 'material_signals': ['CPA', 'ROAS']})
    assert monitor._incident_key(row) != monitor._incident_key({**row, 'intraday': True})


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
