from datetime import date
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from jx_alert.config import Settings
from jx_alert.lingxing import api_business as api
from jx_alert.lingxing.client import LingxingClient, _endpoint_signature_params, LingxingError
from jx_alert.notifications.business_daily import business_daily_card
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store, DailyMetric, DataCoverage, RawSnapshot


DAY = '2026-11-16'


@pytest.fixture
def warehouse(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, store_controls_file=str(tmp_path / 'switches.json'),
        collection_scope_file=str(tmp_path / 'scope-not-set.json'), raw_dir=str(tmp_path / 'raw'),
        app_dry_run=False, excel_archive_enabled=False)
    with Session(engine) as db:
        store = Store(sid='5174', name='Geteen', status=1, timezone='America/Los_Angeles')
        db.add(store)
        db.commit()
        yield db, store, settings
    engine.dispose()


class Client:
    bad_date = False
    empty = False
    def product_performance_daily(self, sid, day, **kwargs):
        return {'code': 0, 'data': {'total': 0 if self.empty else 1, 'list': [] if self.empty else [
            {'sids': [5174], 'asins': [{'sid': '5174', 'asin': 'A'}], 'currency_code': 'USD',
             'volume': 10, 'net_amount': 100, 'spend': 20, 'return_count': 1}]}}
    def profit_report_daily(self, sid, day, **kwargs):
        return {'code': 0, 'data': {'total': 0 if self.empty else 1, 'records': [] if self.empty else [
            {'sid': 5174, 'postedDateLocale': '2026-11-01' if self.bad_date else day, 'currencyCode': 'USD',
             'totalSalesQuantity': 7, 'platformIncome': 80, 'grossProfit': -20, 'grossRate': -.25, 'refundsRate': .1}]}}


def collect(db, settings, client):
    return api.collect_api_business_for_day(client, db, settings, {'data': [{'sid': 5174, 'status': 1}]}, DAY)


def test_api_facts_remain_separate_repeat_collection_preserves_versions_and_cannot_send(warehouse):
    db, store, settings = warehouse
    result = collect(db, settings, Client())
    assert result['status'] == 'COMPLETE' and len(result['partitions']) == 4
    before = db.scalar(select(func.count()).select_from(DailyMetric))
    collect(db, settings, Client())
    assert db.scalar(select(func.count()).select_from(DailyMetric)) == before
    assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 8
    assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    assert set(db.scalars(select(DailyMetric.dimension))) == {api.PRODUCT, api.FINANCE}
    context = api.api_business_context(db, store.sid, DAY)
    assert context['product']['metrics']['volume'] == 10
    assert context['settlement']['metrics']['volume'] == 7
    assert context['settlement']['metrics']['grossProfit'] == -20
    assert '下单日' in context['product']['basis'] and '结算日' in context['settlement']['basis']


def test_exact_finance_failure_preserves_old_values_but_does_not_show_them_as_current_success(warehouse):
    db, store, settings = warehouse
    client = Client()
    collect(db, settings, client)
    client.bad_date = True
    result = collect(db, settings, client)
    assert result['status'] == 'PARTIAL_FAILED'
    context = api.api_business_context(db, store.sid, DAY)
    assert context['settlement'] == {} and context['product']['metrics']['volume'] == 10
    assert db.scalar(select(DailyMetric.value).where(DailyMetric.dimension == api.FINANCE,
        DailyMetric.metric_name == 'grossProfit', DailyMetric.metric_date == date.fromisoformat(DAY))) == -20
    failed = db.scalar(select(RawSnapshot).where(RawSnapshot.payload['status'].as_string() == 'FAILED'))
    assert failed.payload['responses'][0]['data']['records'][0]['postedDateLocale'] == '2026-11-01'


def test_empty_api_results_do_not_create_zero_business_reports(warehouse):
    db, store, settings = warehouse
    client = Client()
    client.empty = True
    collect(db, settings, client)
    assert db.scalar(select(func.count()).select_from(DailyMetric)) == 0
    assert business_daily_card(db, store, DAY) is None


def test_api_daily_card_labels_bases_and_beijing_report_date_without_changing_fact_dates(warehouse):
    db, store, settings = warehouse
    collect(db, settings, Client())
    card = business_daily_card(db, store, DAY, reporting_day='2026-11-17')
    elements = card['body']['elements']
    assert elements[0]['content'] == '2026年11月17日｜美元 USD'
    assert not any(e.get('content') == '****' for e in elements)
    rows = next(e['rows'] for e in elements if e['tag'] == 'table')
    assert len(rows) == 7
    assert rows[0]['c0'] == '销量（下单）' and rows[0]['c1'] == '10'
    assert rows[3]['c1'] == '$-20.00'
    assert rows[5]['c1'] == '20.00%'
    assert '美国统计日：2026-11-16' in elements[-1]['content']
    assert set(db.scalars(select(DailyMetric.metric_date))) == {date(2026, 11, 15), date(2026, 11, 16)}


def test_finance_array_uses_official_sdk_compact_json_without_widening_other_endpoints():
    path = '/bd/profit/report/open/report/seller/list'
    body = {'sids': [5174], 'startDate': DAY}
    assert _endpoint_signature_params(path, body)['sids'] == '[5174]'
    assert body['sids'] == [5174]
    for ids in ([], [1, 2], [True], ['5174']):
        with pytest.raises(LingxingError):
            _endpoint_signature_params(path, {'sids': ids})
    assert _endpoint_signature_params('/unverified', body)['sids'] == [5174]


def test_daily_api_requests_use_fixed_single_store_paths_and_preserve_json_types(monkeypatch):
    client = LingxingClient(base_url='https://openapi.lingxing.com', app_id='synthetic')
    calls = []
    monkeypatch.setattr(client, '_request', lambda method, path, **kwargs: calls.append((path, kwargs['json_body'])))
    client.product_performance_daily('5174', DAY)
    client.profit_report_daily('5174', DAY)
    assert calls[0][0] == '/bd/productPerformance/openApi/asinList'
    assert calls[0][1]['sid'] == '5174' and calls[0][1]['is_recently_enum'] is False
    assert calls[1][1]['sids'] == [5174] and calls[1][1]['orderStatus'] == 'Disbursed'


def test_paused_store_cannot_be_requested_or_have_coverage_rewritten(warehouse, monkeypatch):
    db, store, settings = warehouse
    monkeypatch.setattr(api, 'collection_states', lambda *args: {'5174': False})
    result = collect(db, settings, object())
    assert result['status'] == 'UNAVAILABLE'
    result = collect(db, settings, Client())
    assert result['partitions'] == []
    assert db.scalar(select(func.count()).select_from(DataCoverage)) == 0


def test_morning_beijing_yesterday_title_keeps_the_actual_us_statistical_day(warehouse, monkeypatch):
    from jx_alert.notifications import morning_digest
    db, store, settings = warehouse
    api.collect_api_business_for_day(Client(), db, settings, {'data': [{'sid': 5174, 'status': 1}]}, '2026-11-10')
    monkeypatch.setattr(morning_digest, '_health_chunks', lambda *args: [])
    monkeypatch.setattr(morning_digest, 'sp_campaign_exceptions', lambda *args: ([], None))
    cards = morning_digest.morning_cards(db, settings, [store], {'5174': '2026-11-10'}, send_day=date(2026, 11, 12))
    value = cards[0][0]
    assert value['body']['elements'][0]['content'] == '2026年11月11日｜美元 USD'
    assert '美国统计日：2026-11-10' in value['body']['elements'][-1]['content']
    assert api.api_business_context(db, '5174', '2026-11-11')['product'] == {}


def test_model_omits_repetitive_missing_rows_but_keeps_real_zero_and_distinct_api_evidence():
    from copy import deepcopy
    from jx_alert.ai.deepseek import compact_context
    context = {'advertising_kpis': {}, 'business_daily': {'availability': 'MISSING',
        'missing_metrics': ['销量', '净销售额'], 'metrics': [
            {'label': '销量', 'current': None, 'previous': None},
            {'label': '净销售额', 'current': 0, 'previous': None}]},
        'api_business_observations': {'product': {'metrics': {'volume': 10}, 'basis': '下单日'}}}
    original = deepcopy(context)
    packet = compact_context(context)
    assert packet['business_daily']['metrics'] == [{'label': '净销售额', 'current': 0, 'previous': None}]
    assert 'missing_metrics' not in packet['business_daily']
    assert packet['api_business_observations'] == context['api_business_observations']
    assert context == original


def test_api_backfill_runs_without_mcp_and_exports_both_exact_dates(warehouse, monkeypatch):
    db, store, settings = warehouse
    spec = importlib.util.spec_from_file_location('business_backfill', Path(__file__).parents[1] / 'scripts/backfill_business.py')
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)
    client = Client()
    client.get_stores = lambda: {'data': [{'sid': 5174, 'status': 1}]}
    monkeypatch.setattr(backfill, 'get_settings', lambda: settings)
    monkeypatch.setattr(backfill.LingxingClient, 'from_settings', lambda: client)
    monkeypatch.setattr(backfill, 'engine', db.get_bind())
    monkeypatch.setattr(backfill, 'SessionLocal', lambda: Session(db.get_bind()))
    monkeypatch.setattr(backfill, 'collect_business_for_day', lambda *args: pytest.fail('API-only backfill cannot call MCP'))
    archived = []
    monkeypatch.setattr(backfill, 'archive_partitions', lambda db, settings, dates: archived.append(dates) or {'status': 'COMPLETE'})
    result = backfill.backfill(DAY, 'api', ['5174'])
    assert result['status'] == 'COMPLETE'
    assert archived == [{'5174': ['2026-11-15', '2026-11-16']}]
    assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
