"""Bulk queue selection and progress do not need provider access or new data tables."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.admin_console import OwnerConsole, ConsoleError
from jx_alert.config import Settings
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store, SyncJob
from jx_alert.store_controls import get_store_controls, save_store_controls


@pytest.fixture
def bulk(tmp_path, monkeypatch):
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'store_ids': [5174, 5175, 5176, 5177, 5178, 5179, 5180]}))
    zones = tmp_path / 'zones.json'
    zones.write_text(json.dumps({'stores': {sid: {'timezone': 'America/Los_Angeles'}
        for sid in ('5174', '5176', '5180')}}))
    settings = Settings(_env_file=None, app_dry_run=False, scheduler_enabled=True, mcp_operations_enabled=True,
        collection_scope_file=str(scope), store_timezones_file=str(zones), store_controls_file=str(tmp_path / 'controls.json'))
    import jx_alert.config as config
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        db.add_all([Store(sid=sid, name='店铺' + sid, status=status) for sid, status in (
            ('5174', 1), ('5175', 2), ('5176', 1), ('5177', 0), ('5179', 1), ('5180', 1), ('9999', 1))])
    save_store_controls(settings, '5176', False, True, get_store_controls(settings, '5176')['revision'])
    # Notifications do not control read-only collection.
    save_store_controls(settings, '5180', True, False, get_store_controls(settings, '5180')['revision'])
    provider = SimpleNamespace(_password_valid=lambda _: False, issuer_url='https://example.test/',
                               database_path=tmp_path / 'oauth.sqlite')
    console = OwnerConsole(settings, provider, session_factory=sessions)
    yield console, settings, sessions
    engine.dispose()


def test_bulk_queues_only_collectable_stores_and_freezes_skipped_reasons(bulk):
    console, settings, sessions = bulk
    original = Path(settings.store_controls_file).read_bytes()
    first = console._bulk_collection(enqueue=True)
    assert first['status'] == 'PENDING' and not first['reused']
    assert first['selected_store_count'] == 2 and first['skipped_store_count'] == 5
    assert first['skipped_reason_counts'] == {'授权失败': 1, '采集已关闭': 1, '店铺未启用': 1,
                                              '尚未同步店铺': 1, '报表日期或时区待确认': 1}
    assert {r['store_id'] for r in first['stores']} == {'5174', '5180'}
    second = console._bulk_collection(enqueue=True)
    assert second['job_id'] == first['job_id'] and second['reused']
    assert Path(settings.store_controls_file).read_bytes() == original
    with sessions() as db:
        jobs = list(db.scalars(select(SyncJob).where(SyncJob.job_type == 'manual_collection')))
        assert len(jobs) == 1 and jobs[0].metadata_json['store_ids'] == ['5174', '5180']
        assert jobs[0].metadata_json['report_date'] is None and jobs[0].metadata_json['backfill_days'] == 2


def test_inflight_bulk_is_reused_without_overwriting_original_subset_or_skips(bulk):
    console, settings, sessions = bulk
    first = console._bulk_collection(enqueue=True)
    with sessions.begin() as db:
        job = db.get(SyncJob, first['job_id'])
        job.status = 'RUNNING'
        original = dict(job.metadata_json)
    save_store_controls(settings, '5174', False, False, get_store_controls(settings, '5174')['revision'])
    again = console._bulk_collection(enqueue=True)
    assert again['job_id'] == first['job_id'] and again['reused']
    assert again['paused_store_count'] == 1
    waiting = next(row for row in again['stores'] if row['store_id'] == '5180')
    assert waiting['status'] == 'AWAITING_RESULT' and waiting['status_label'] == '等待逐店结果'
    with sessions() as db:
        assert db.get(SyncJob, first['job_id']).metadata_json == original


def test_bulk_receipt_reports_real_checkpoints_and_never_exposes_raw_metadata(bulk):
    console, _, sessions = bulk
    first = console._bulk_collection(enqueue=True)
    with sessions.begin() as db:
        job = db.get(SyncJob, first['job_id'])
        job.metadata_json = {**job.metadata_json, 'private_marker': 'must-not-leak',
            'completed_store_dates': {'5174': '2026-11-14'}, 'store_statuses': {'5180': 'PARTIAL'},
            'retry_at': '2026-11-16T10:00:00', 'planned_store_dates': {'5174': '2026-11-14', '5180': '2026-11-14'},
            'result': {'raw_payload': 'must-not-leak'}}
    receipt = console._bulk_collection()
    assert receipt['completed_store_count'] == 1 and receipt['remaining_store_count'] == 1
    assert receipt['status_label'] == '等待重试' and receipt['retry_at'] == '2026-11-16T10:00:00'
    assert all(row['report_date'] == '2026-11-14' for row in receipt['stores'])
    assert 'must-not-leak' not in json.dumps(receipt)
    assert all(set(row) == {'store_id', 'store_name', 'status', 'status_label', 'report_date'} for row in receipt['stores'])


def test_finished_bulk_is_not_rearmed_and_new_click_creates_a_new_request(bulk):
    console, _, sessions = bulk
    first = console._bulk_collection(enqueue=True)
    with sessions.begin() as db:
        db.get(SyncJob, first['job_id']).status = 'FAILED'
    second = console._bulk_collection(enqueue=True)
    assert second['job_id'] != first['job_id'] and not second['reused']
    with sessions() as db:
        assert db.get(SyncJob, first['job_id']).status == 'FAILED'


def test_all_ineligible_returns_skip_counts_without_creating_work(bulk):
    console, settings, sessions = bulk
    for sid in ('5174', '5180'):
        save_store_controls(settings, sid, False, False, get_store_controls(settings, sid)['revision'])
    result = console._bulk_collection(enqueue=True)
    assert result['status'] == 'NO_ELIGIBLE' and result['job_id'] is None
    assert result['selected_store_count'] == 0 and result['skipped_store_count'] == 7
    with sessions() as db:
        assert list(db.scalars(select(SyncJob))) == []


def test_bulk_selection_cannot_bypass_service_gates(bulk):
    console, settings, sessions = bulk
    settings.mcp_operations_enabled = False
    with pytest.raises(ConsoleError, match='手动同步服务尚未开启'):
        console._bulk_collection(enqueue=True)
    with sessions() as db:
        assert list(db.scalars(select(SyncJob))) == []


def test_bulk_progress_hides_stores_removed_from_the_current_scope(bulk):
    console, settings, _ = bulk
    console._bulk_collection(enqueue=True)
    Path(settings.collection_scope_file).write_text(json.dumps({'store_ids': [5174]}))
    result = console._bulk_collection()
    assert result['selected_store_count'] == 1 and result['skipped_store_count'] == 0
    assert [r['store_id'] for r in result['stores']] == ['5174']


def test_bulk_receipt_survives_worker_progress_for_a_reused_job(bulk):
    from jx_alert.operations import enqueue_collection
    console, _, sessions = bulk
    with sessions.begin() as db:
        original = enqueue_collection(db, store_ids=['5174', '5180'], backfill_days=2)
        job = db.get(SyncJob, original['job_id'])
        job.status = 'RUNNING'
        worker_metadata = dict(job.metadata_json)
    attached = console._bulk_collection(enqueue=True)
    assert attached['reused'] and attached['job_id'] == original['job_id']
    with sessions.begin() as db:
        job = db.get(SyncJob, original['job_id'])
        assert job.metadata_json == worker_metadata
        job.metadata_json = {**worker_metadata, 'completed_store_dates': {'5174': '2026-11-14'}}
    read_back = console._bulk_collection()
    assert read_back['job_id'] == original['job_id']
    assert read_back['completed_store_count'] == 1 and read_back['skipped_store_count'] == 5
