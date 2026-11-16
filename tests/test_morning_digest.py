"""Meaningful daily briefs keep source quality, maturity and delivery boundaries."""
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications.daily_report import enqueue_daily_snapshots
from jx_alert.notifications.morning_digest import morning_cards, sp_campaign_exceptions
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, PerformanceNotice, Store, StorePerformance, SyncJob
from jx_alert.store_controls import get_store_controls, save_store_controls


DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


@pytest.fixture
def warehouse(tmp_path):
    routing = tmp_path / 'routing.json'
    routing.write_text(json.dumps({'default_chat_id': 'reports', 'groups': {'reports': {'chat_id': 'oc_reports'}}}))
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport='app', feishu_daily_report_enabled=True,
        feishu_daily_report_chat_id='reports', feishu_routing_file=str(routing),
        store_controls_file=str(tmp_path / 'controls.json'), lingxing_performance_source_timezone='',
        lingxing_performance_observation_only=True)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        store = Store(sid='5174', name='Geteen美国', status=1, currency_code='USD')
        db.add(store)
        db.add(StorePerformance(store_id=store.sid, pull_date=DAY, ahr_status='GREAT', ahr_score=256,
            source_updated_at='2026-11-14 06:56:17', indicators={}, standards={}))
        db.add(Campaign(store_id=store.sid, campaign_id='a', campaign_type='sp', state='enabled', name='普通活动'))
        for i in range(15):
            on = DAY - timedelta(days=i)
            db.add(DataCoverage(store_id=store.sid, metric_date=on, dimension='campaign_sp', status='COMPLETE', row_count=1))
            for metric, value in {'spend': 100, 'orders': 10, 'units': 12, 'sales': 500, 'clicks': 100, 'impressions': 10000}.items():
                db.add(DailyMetric(store_id=store.sid, entity_id='a', dimension='campaign_sp', metric_date=on,
                    metric_name=metric, value=value, currency='USD' if metric in {'sales', 'spend'} else None))
        db.commit()
        yield db, settings, store
    engine.dispose()


def change(db, metric, value, on=DAY):
    row = db.scalar(select(DailyMetric).where(DailyMetric.metric_date == on, DailyMetric.metric_name == metric))
    row.value = value
    db.commit()


def render(db, settings, store):
    return morning_cards(db, settings, [store], {store.sid: DAY}, send_day=NOW.date(), now=NOW)


def test_daily_health_is_sent_without_normal_advertising_or_empty_optional_types(warehouse):
    db, settings, store = warehouse
    chunks = render(db, settings, store)
    assert len(chunks) == 1
    text = json.dumps(chunks, ensure_ascii=False)
    assert '店铺绩效与违规快报' in text and '源时区待确认' in text and '评级良好（源观测）' in text
    assert '当前判定' in text and '待确认' in text
    assert all(term not in text for term in ('广告无异常', '品牌推广', '展示推广', '商品推广活动待复核'))


def test_quiet_morning_keeps_health_observations_and_omits_separate_data_gap_message(warehouse):
    db, settings, store = warehouse
    settings.feishu_quiet_delivery_enabled = True
    missing = DAY + timedelta(days=1)
    result = enqueue_daily_snapshots(db, settings, store_dates={store.sid: missing},
        delivery_date=NOW.date(), now=NOW, focused_morning=True)
    assert len(result['outbox_ids']) == 1
    row = db.get(NotificationOutbox, result['outbox_ids'][0])
    assert row.payload['morning_message_kind'] == 'health'
    text = json.dumps(row.payload['card'], ensure_ascii=False)
    assert '源时区待确认' not in text and '时效待确认' in text
    assert '2026-11-14 06:56:17' in text and '当前判定：待确认' in text
    assert any(chunk[0]['header']['title']['content']=='广告判断待补齐数据' for chunk in morning_cards(
        db, settings, [store], {store.sid: missing}, send_day=NOW.date(), now=NOW))


def test_only_mature_zero_conversion_generates_a_specific_campaign_problem_with_all_kpis(warehouse):
    db, settings, store = warehouse
    change(db, 'orders', 0)  # Yesterday's immature zero is not a no-conversion warning.
    assert len(render(db, settings, store)) == 1
    change(db, 'orders', 0, DAY - timedelta(days=7))
    chunks = render(db, settings, store)
    assert len(chunks) == 2
    text = json.dumps(chunks[1], ensure_ascii=False)
    assert '成熟广告有花费但无归因订单' in text and '2026-11-05' in text and '普通活动' in text
    assert all(code in text for code in ('CTR', 'CPC', 'CDR', 'CPS', 'ROAS'))
    assert len(sp_campaign_exceptions(db, settings, store, DAY)[0][0]['kpis']) == 7


def test_improvements_and_small_changes_do_not_generate_campaign_messages(warehouse):
    db, settings, store = warehouse
    for on in (DAY - timedelta(days=7), DAY - timedelta(days=8)):
        change(db, 'orders', 20, on)
        change(db, 'sales', 1000, on)
    change(db, 'spend', 110)
    assert len(render(db, settings, store)) == 1


def test_material_click_cost_has_specific_topic_instead_of_order_anomaly(warehouse):
    db, settings, store = warehouse
    change(db, 'spend', 210)
    findings, quality = sp_campaign_exceptions(db, settings, store, DAY)
    assert quality is None and len(findings) == 1
    topics = [t[0] for t in findings[0]['topics']]
    assert '点击花费上升' in topics
    assert '成熟归因订单下降' not in topics


@pytest.mark.parametrize('state', ['no_records', 'uncollected', 'failed', 'unauthorized'])
def test_missing_data_is_separate_from_business_findings_and_never_normal(warehouse, state):
    db, settings, store = warehouse
    if state in {'no_records', 'uncollected'}:
        for row in list(db.scalars(select(DailyMetric).where(DailyMetric.metric_date == DAY))):
            db.delete(row)
        coverage = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY))
        if state == 'uncollected':
            db.delete(coverage)
        else:
            coverage.row_count = 0
    elif state == 'failed':
        db.add(SyncJob(job_type='daily_collection', status='FAILED', metadata_json={'report_dates': {'5174': DAY.isoformat()}}))
    else:
        store.status = 2
    db.commit()
    text = json.dumps(render(db, settings, store), ensure_ascii=False)
    assert '广告判断待补齐数据' in text and '商品推广活动待复核' not in text
    expected = {'no_records': '接口无记录', 'uncollected': '尚未完整采集', 'failed': '采集失败', 'unauthorized': '授权'}[state]
    assert expected in text


def test_pending_generic_notice_is_not_mislabeled_a_confirmed_violation(warehouse):
    db, settings, store = warehouse
    db.add(PerformanceNotice(store_id=store.sid, notice_key='n1', status_label='PENDING', subject='请查看店铺通知'))
    db.commit()
    text = json.dumps(render(db, settings, store), ensure_ascii=False)
    assert '1条通知待处理，性质待核查' in text and '1条违规' not in text
    db.scalar(select(PerformanceNotice)).subject = '知识产权侵权通知'
    db.commit()
    assert '1条违规相关通知待核查' in json.dumps(render(db, settings, store), ensure_ascii=False)


def test_complete_partition_with_missing_campaign_metrics_is_still_a_quality_gap(warehouse):
    db, settings, store = warehouse
    db.delete(db.scalar(select(DailyMetric).where(DailyMetric.metric_date == DAY, DailyMetric.metric_name == 'impressions')))
    db.commit()
    findings, quality = sp_campaign_exceptions(db, settings, store, DAY)
    assert findings == [] and '目标日关键指标不完整' in quality


def test_focused_schedule_preserves_disabled_stores_and_reuses_frozen_day(warehouse):
    db, settings, store = warehouse
    paused = Store(sid='5175', name='已关闭加拿大', status=1)
    db.add(paused)
    db.commit()
    save_store_controls(settings, paused.sid, False, False, get_store_controls(settings, paused.sid)['revision'])
    kwargs = dict(store_dates={store.sid: DAY, paused.sid: DAY}, delivery_date=NOW.date(), now=NOW, focused_morning=True)
    first = enqueue_daily_snapshots(db, settings, **kwargs)
    db.commit()
    ids = first['outbox_ids']
    rows = list(db.scalars(select(NotificationOutbox)))
    assert len(rows) == 1 and rows[0].payload['store_ids'] == ['5174']
    assert rows[0].payload['scheduled_not_before'] == '2026-11-14T08:00:00+08:00'
    assert rows[0].payload['store_report_dates'] == {'5174': {'target': DAY.isoformat(), 'actual': DAY.isoformat()}}
    rows[0].status = 'DELIVERED'
    db.commit()
    change(db, 'spend', 210)
    assert enqueue_daily_snapshots(db, settings, **kwargs)['outbox_ids'] == ids
    assert rows[0].status == 'DELIVERED'


def test_upgrade_does_not_resend_old_format_for_the_same_delivery_day(warehouse):
    db, settings, store = warehouse
    db.add(NotificationOutbox(idempotency_key='scheduled-daily:old-format', chat_id='oc_reports', status='DELIVERED',
        payload={'scheduled_delivery_date': NOW.date().isoformat()}))
    db.commit()
    old_id = db.scalar(select(NotificationOutbox.id))
    result = enqueue_daily_snapshots(db, settings, store_dates={store.sid: DAY}, delivery_date=NOW.date(),
        now=NOW, focused_morning=True)
    assert result['outbox_ids'] == [old_id]


@pytest.mark.parametrize('failed', [False, True])
def test_unknown_timezone_history_fallback_keeps_actual_date_and_never_masks_target_failure(warehouse, failed):
    db, settings, store = warehouse
    for dimension, count in [('store', 1), ('campaign_sb', 0), ('campaign_sd', 0)]:
        db.add(DataCoverage(store_id=store.sid, dimension=dimension, metric_date=DAY, status='COMPLETE', row_count=count))
    for metric, value in {'orders': 10, 'units': 12, 'sales': 500}.items():
        db.add(DailyMetric(store_id=store.sid, dimension='store', entity_id=store.sid, metric_date=DAY,
            metric_name=metric, value=value, currency='USD' if metric == 'sales' else None))
    target = DAY + timedelta(days=1)
    if failed:
        db.add(SyncJob(job_type='daily_collection', status='FAILED', metadata_json={'report_dates': {store.sid: target.isoformat()}}))
    db.commit()
    change(db, 'orders', 0, DAY - timedelta(days=7))
    result = enqueue_daily_snapshots(db, settings, store_dates={store.sid: target}, delivery_date=NOW.date(),
        now=NOW, focused_morning=True, history_fallback_store_ids={store.sid})
    payloads = [db.get(NotificationOutbox, i).payload for i in result['outbox_ids']]
    assert all(p['store_report_dates'][store.sid] == {'target': str(target), 'actual': str(target if failed else DAY)} for p in payloads)
    text = json.dumps(payloads, ensure_ascii=False)
    if failed:
        assert '采集失败' in text and '商品推广活动待复核' not in text and '仅复核历史' not in text
    else:
        assert '商品推广活动待复核' in text and '仅复核历史报表日2026-11-12' in text
        assert '目标报表日' in text and '实际复核日' in text
