"""Operational messages fit narrow chat cards without losing evidence or truth."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.analysis.ad_kpis import compare_kpis
from jx_alert.bot_intents import inventory_cards
from jx_alert.notifications.cards import compact_ad_kpi_rows
from jx_alert.notifications.morning_digest import morning_cards, sp_campaign_exceptions
from jx_alert.notifications.store_health import card_fits
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, Store, StorePerformance


DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def observations():
    return {DAY - timedelta(days=i): {'spend': 100, 'sales': 500, 'orders': 10, 'units': 12,
        'clicks': 100, 'impressions': 10000, 'currency': 'USD', 'sales_currency': 'USD'} for i in range(15)}


def tables(value):
    return [element for element in value['body']['elements'] if element.get('tag') == 'table']


def test_compact_kpis_retain_value_dates_reference_changes_and_real_zero_without_mutation():
    kpis = compare_kpis(observations(), DAY, maturity_days=7, store_scope=False)
    kpis['CTR'].update(current=0, change_ratio=-1, status='ADVERSE')
    before = deepcopy(kpis)
    rows = compact_ad_kpi_rows(kpis)
    assert len(rows) == 6 and all(len(row) == 4 for row in rows)
    ctr = next(row for row in rows if 'CTR' in row[0])
    assert '0.00%' in ctr[1] and str(DAY) in ctr[1]
    assert '七日' in ctr[2] and '-100.0%' in ctr[2] and ctr[3] == '恶化待复核'
    cdr = next(row for row in rows if 'CDR' in row[0])
    assert '2026-11-05' in cdr[1]
    assert not any('TCOAS' in row[0] for row in rows)
    assert len(compact_ad_kpi_rows(kpis, omit_empty=False)) == 7 and kpis == before


@pytest.fixture
def warehouse(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(store_controls_file=str(tmp_path / 'controls.json'),
        lingxing_performance_source_timezone='', lingxing_performance_observation_only=True)
    with sessionmaker(engine)() as db:
        stores = [Store(sid=sid, name=name, country='美国', status=1, currency_code='USD')
                  for sid, name in [('5174', 'Geteen美国'), ('5175', 'Campx美国')]]
        db.add_all(stores)
        db.add(StorePerformance(store_id='5174', pull_date=DAY, ahr_status='GREAT', ahr_score=256,
            source_updated_at='2026-11-14 06:56:17', indicators={'order_with_defect': '2'},
            standards={'orderWithDefect': '1'}))
        db.add(Campaign(store_id='5174', campaign_id='a', campaign_type='sp', name='具体待复核活动', state='enabled'))
        for on, values in observations().items():
            db.add(DataCoverage(store_id='5174', dimension='campaign_sp', metric_date=on, status='COMPLETE', row_count=1))
            for metric in ('spend', 'sales', 'orders', 'units', 'clicks', 'impressions'):
                value = 0 if metric == 'orders' and on == DAY - timedelta(days=7) else values[metric]
                db.add(DailyMetric(store_id='5174', dimension='campaign_sp', entity_id='a', metric_date=on,
                    metric_name=metric, value=value, currency='USD' if metric in {'spend', 'sales'} else None))
        db.commit()
        yield db, settings, stores
    engine.dispose()


def test_joint_brief_keeps_all_health_first_and_only_material_sp_then_quality_with_narrow_tables(warehouse):
    db, settings, stores = warehouse
    before = db.scalar(select(StorePerformance)).source_updated_at
    chunks = morning_cards(db, settings, stores, {s.sid: DAY for s in stores}, send_day=NOW.date(), now=NOW)
    headers = [value['header']['title']['content'] for value, _ in chunks]
    assert headers[0].startswith('店铺绩效与违规快报') and headers[1] == '商品推广活动待复核'
    assert headers[-1] == '广告判断待补齐数据'
    health = tables(chunks[0][0])[0]
    assert {r['c0'] for r in health['rows']} == {'Geteen美国', 'Campx美国'}
    health_text = json.dumps(health, ensure_ascii=False)
    assert '评分 256 · 评级良好（观测）' in health_text and '当前判定' not in health_text
    assert '源时区待确认' not in health_text and '订单缺陷率 2%' in health_text
    assert '未见违规' not in health_text and '采集时间（UTC）' not in health_text
    for value, _ in chunks:
        assert card_fits(value) and all(len(t['columns']) <= 4 for t in tables(value))
    assert db.scalar(select(StorePerformance)).source_updated_at == before
    findings, _ = sp_campaign_exceptions(db, settings, stores[0], DAY)
    assert len(findings[0]['kpis']) == 7  # Message reduction does not remove stored/queryable dimensions.


def test_inventory_omits_switches_and_empty_business_columns_without_claiming_source_zero(warehouse):
    db, settings, stores = warehouse
    values = inventory_cards(db, settings, stores)
    result = tables(values[0])[0]
    assert [column['display_name'] for column in result['columns']] == ['店铺', '最近可用报表日', '销量', '商品推广']
    usable = next(row for row in result['rows'] if row['c0'] == 'Geteen美国')
    assert usable['c1'] == DAY.isoformat() and usable['c2'] == '尚未采集' and usable['c3'] == '有广告记录'
    missing = next(row for row in result['rows'] if row['c0'] == 'Campx美国')
    assert missing['c1'] == '暂无可用记录' and missing['c2'] == '尚未采集'
    serialized = json.dumps(values, ensure_ascii=False)
    assert not any(term in serialized for term in ['采集开', '采集关', '通知开', '通知关'])
    assert all(card_fits(value) for value in values)


def test_single_store_unauthorized_health_has_one_actionable_row_without_empty_boilerplate():
    from jx_alert.bot_presentation import operator_card
    context = {'store_name': 'CROSSLAND LLC-US', 'question_topic': 'health', 'health': {
        'status': 'AUTHORIZATION_FAILED', 'confirmation_reason': '店铺授权异常',
        'ahr_score': None, 'source_status_label': '未提供', 'source_updated_at': None,
        'pending_notice_observations': 0, 'indicators': [], 'freshness_label': '无记录'}}
    result = operator_card(context)
    rows = tables(result)[0]['rows']
    assert len(rows) == 1 and '恢复授权后同步' in rows[0]['c1']
    text = json.dumps(result, ensure_ascii=False)
    assert not any(label in text for label in ('未知', '未提供', '归档未见', '健康评分', '源时区'))


def test_single_store_health_keeps_real_observation_and_risk_without_normal_metric_dump():
    from jx_alert.bot_presentation import operator_card
    context = {'store_name': 'Geteen-US', 'question_topic': 'health', 'health': {
        'status': 'UNCONFIRMED', 'confirmation_reason': '源时区待确认',
        'ahr_score': 256, 'source_status_label': '评级良好', 'source_updated_at': '2026-11-15 15:47:50',
        'pending_notice_observations': 0, 'indicators': [
            {'label': '订单缺陷率', 'value': 0, 'standard': 1, 'status': 'WITHIN_STANDARD', 'status_label': '未见超标'},
            {'label': '迟发率', 'value': 6, 'standard': 4, 'status': 'BREACHED', 'status_label': '观测未达标'}]}}
    before = deepcopy(context)
    result = operator_card(context)
    text = json.dumps(result, ensure_ascii=False)
    assert '256' in text and '归档观测' in text and '迟发率' in text and '6%' in text
    assert '当前健康' in text and '订单缺陷率' not in text and '待处理通知' not in text
    assert '2026-11-15 15:47:50' in text and context == before
