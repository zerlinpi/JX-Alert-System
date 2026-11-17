from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.ai.analyst_evidence import analysis_evidence
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store, SyncJob


END = date(2026, 11, 16)
QUESTION = 'Geteen美国按当前速度预测月底SP广告花费'


@pytest.fixture
def observations():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(sid='5174', name='Geteen-US', status=1)
        db.add(store)
        for day in range(1, 17):
            on = END.replace(day=day)
            db.add(DataCoverage(store_id=store.sid, metric_date=on, dimension='campaign_sp',
                status='COMPLETE', row_count=1))
            for metric, value in {'spend': 100, 'clicks': 50, 'impressions': 1000}.items():
                db.add(DailyMetric(store_id=store.sid, metric_date=on, dimension='campaign_sp',
                    entity_id='campaign', metric_name=metric, value=value,
                    currency='USD' if metric == 'spend' else None))
        db.commit()
        yield db, store
    engine.dispose()


def test_projection_is_explicit_readonly_scenario_from_seven_complete_days(observations):
    db, store = observations
    evidence = analysis_evidence(db, store, END, QUESTION, completed=END)
    projection = evidence['spend_projection']
    assert projection['scenario_only'] is True
    assert projection['month_to_date'] == 1600
    assert projection['remaining_days'] == 14
    assert projection['remaining_spend'] == 1400
    assert projection['monthly_total'] == 3000
    assert projection['basis_start'] == '2026-11-10' and projection['basis_end'] == str(END)
    assert projection['observed_pace_range'] == [1400, 1400]
    assert projection['budget_limit'] is None
    assert db.scalar(select(SyncJob.id)) is None
    assert not db.new and not db.dirty and not db.deleted


def test_missing_early_month_does_not_become_zero_or_a_monthly_total(observations):
    db, store = observations
    row = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == END.replace(day=3)))
    row.status = 'FAILED'
    db.commit()
    evidence = analysis_evidence(db, store, END, QUESTION, completed=END)
    projection = evidence['spend_projection']
    assert evidence['missing_days'] == [{'date': '2026-11-03', 'reason': '广告采集失败'}]
    assert projection['month_to_date'] is None and projection['monthly_total'] is None
    assert projection['remaining_spend'] == 1400
    assert '缺采' in projection['gap']


@pytest.mark.parametrize('condition', ['currency', 'missing', 'no_records', 'failure'])
def test_invalid_recent_pace_blocks_forecasts_without_filling_gaps(observations, condition):
    db, store = observations
    row = db.scalar(select(DailyMetric).where(DailyMetric.metric_date == END, DailyMetric.metric_name == 'spend'))
    coverage = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == END))
    if condition == 'currency':
        row.currency = 'EUR'
    elif condition == 'missing':
        db.delete(row)
    elif condition == 'no_records':
        coverage.row_count = 0
        for fact in db.scalars(select(DailyMetric).where(DailyMetric.metric_date == END)):
            db.delete(fact)
    else:
        db.add(SyncJob(job_type='daily_collection', status='FAILED',
            metadata_json={'report_dates': {store.sid: str(END)}}))
    db.commit()
    evidence = analysis_evidence(db, store, END, QUESTION, completed=END)
    projection = evidence['spend_projection']
    assert projection['remaining_spend'] is None and projection['monthly_total'] is None
    assert '不足' in projection['gap']


def test_future_query_uses_only_completed_report_days(observations):
    db, store = observations
    evidence = analysis_evidence(db, store, END + timedelta(days=1), QUESTION, completed=END)
    assert evidence['through_report_date'] == str(END)
    assert evidence['daily_spend'][-1]['date'] == str(END)
    assert evidence['spend_projection']['basis_end'] == str(END)


def test_unauthorized_store_has_no_projected_current_business(observations):
    db, store = observations
    store.status = 2
    db.commit()
    evidence = analysis_evidence(db, store, END, QUESTION, completed=END)
    assert 'spend_projection' not in evidence
    assert evidence['daily_spend'] == []
    assert '授权' in evidence['limitations'][0]


def test_extreme_finite_observations_cannot_create_infinite_projection(observations):
    db, store = observations
    for row in db.scalars(select(DailyMetric).where(DailyMetric.metric_name == 'spend')):
        row.value = 1e308
    db.commit()
    projection = analysis_evidence(db, store, END, QUESTION, completed=END)['spend_projection']
    assert projection['monthly_total'] is None and projection['remaining_spend'] is None
    assert '超出' in projection['gap']


def test_unsupported_inventory_and_keyword_requests_name_missing_evidence(observations):
    db, store = observations
    evidence = analysis_evidence(db, store, END, '预测库存补货，哪个搜索词浪费，分析分时', completed=END)
    assert 'spend_projection' not in evidence
    assert any('交期' in gap for gap in evidence['limitations'])
    assert any('搜索词' in gap for gap in evidence['limitations'])
    assert any('小时明细' in gap for gap in evidence['limitations'])
