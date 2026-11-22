"""Health briefs show concrete observations and one actionable blocker per store."""
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert.notifications.morning_digest import _health_chunks, morning_health_row
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AnomalyEvent, PerformanceNotice, PerformanceSyncState, Store, StorePerformance,
)


NOW = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def warehouse():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(lingxing_performance_source_timezone='',
        lingxing_performance_observation_only=True, feishu_quiet_delivery_enabled=True)
    with Session(engine) as db:
        store = Store(sid='1', name='CROSSLAND LLC-US', status=1)
        db.add(store)
        db.flush()
        yield db, settings, store
    engine.dispose()


def text(db, settings, stores):
    return json.dumps(_health_chunks(db, settings, stores, NOW.date(), NOW), ensure_ascii=False)


def test_authorization_failure_has_action_without_empty_scores_timestamps_or_health_claim(warehouse):
    db, settings, store = warehouse
    store.status = 2
    result = text(db, settings, [store])
    assert 'CROSSLAND LLC-US' in result and '店铺授权失效' in result and '在领星重新授权' in result
    assert all(word not in result for word in (
        '未知', '无记录', '未提供', '源更新时间', '归档未见待处理通知', '源时区', '健康评分', '健康正常',
    ))
    assert '观测值未确认' not in result
    assert db.scalar(select(func.count(AnomalyEvent.id))) == 0


@pytest.mark.parametrize('failed', [False, True])
def test_unavailable_health_is_one_data_blocker_and_does_not_imply_healthy_or_zero(warehouse, failed):
    db, settings, store = warehouse
    if failed:
        db.add(PerformanceSyncState(store_id=store.sid, dataset='store_performance', status='FAILED',
            captured_at=NOW.replace(tzinfo=None)))
        db.flush()
    result = morning_health_row(db, settings, store, now=NOW)
    assert result == (store.name, '绩效采集失败' if failed else '未取得绩效数据', '同步绩效后复核')
    assert not any('健康' in cell or '0' == cell for cell in result)


@pytest.mark.parametrize('authorized', [False, True])
def test_known_pending_violation_survives_missing_health_or_failed_authorization(warehouse, authorized):
    db, settings, store = warehouse
    store.status = 1 if authorized else 2
    db.add(PerformanceNotice(store_id=store.sid, notice_key='notice', status_label='PENDING',
        subject='知识产权侵权'))
    db.flush()
    result = text(db, settings, [store])
    assert '1条违规相关通知待核查：知识产权侵权' in result
    assert '已确认违规' not in result and '未见待处理违规通知' not in result


def test_zero_score_and_real_breach_retained_without_empty_standards_or_repeated_time_warning(warehouse):
    db, settings, store = warehouse
    source = '2026-11-16 16:00:00'
    db.add(StorePerformance(store_id=store.sid, pull_date=date(2026, 11, 16), ahr_score=0,
        ahr_status='AtRisk', source_updated_at=source, indicators={'late_shipment': '8'},
        standards={'lateShipment': '4'}))
    db.flush()
    result = text(db, settings, [store])
    assert '评分 0 · 有风险（观测）' in result and '迟发率 8%（标准 4%）' in result
    assert '源时区待确认' not in result and '部分绩效指标待补齐' not in result
    assert db.scalar(select(StorePerformance)).source_updated_at == source
    assert db.scalar(select(func.count(AnomalyEvent.id))) == 0


@pytest.mark.parametrize('age,issue', [(timedelta(days=2), '绩效更新滞后'),
    (timedelta(minutes=-30), '绩效源时间异常')])
def test_stale_and_future_observations_keep_specific_quality_issue(warehouse, age, issue):
    db, settings, store = warehouse
    db.add(StorePerformance(store_id=store.sid, pull_date=NOW.date(), ahr_score=256,
        ahr_status='GREAT', source_updated_at=(NOW - age).isoformat(), indicators={}, standards={}))
    db.flush()
    row = morning_health_row(db, settings, store, now=NOW)
    assert '评分 256 · 评级良好（观测）' == row[1] and issue in row[2]


def test_health_pages_remain_narrow_and_preserve_every_store(warehouse):
    db, settings, store = warehouse
    stores = [store] + [Store(sid=str(i), name=f'店铺{i}', status=2) for i in range(2, 23)]
    db.add_all(stores[1:])
    db.flush()
    pages = _health_chunks(db, settings, stores, NOW.date(), NOW)
    assert [sid for _, ids in pages for sid in ids] == [s.sid for s in stores]
    tables = [element for value, _ in pages for element in value['body']['elements'] if element['tag'] == 'table']
    assert len(tables) == 3 and all(len(table['columns']) == 3 for table in tables)


def test_proactive_unknown_source_time_keeps_observation_without_breach_assertion(warehouse):
    db, settings, store = warehouse
    db.add(StorePerformance(store_id=store.sid, pull_date=NOW.date(), ahr_score=256,
        ahr_status='AtRisk', source_updated_at='2026-11-16 16:00:00',
        captured_at=NOW.replace(tzinfo=None), indicators={'late_shipment': '8'},
        standards={'lateShipment': '4'}))
    db.flush()
    row = morning_health_row(db, settings, store, now=NOW, proactive=True)
    assert row[1] == '评分 256 · 有风险（观测）'
    assert '更新时间未核实' in row[2] and '不判定当前异常' in row[2]
    assert '迟发率' not in row[2] and '标准 4%' not in row[2]
