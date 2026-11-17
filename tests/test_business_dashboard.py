"""Official dashboard partitions keep accounting grain, evidence and read-only scope."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert.lingxing import business_dashboard as dashboard
from jx_alert.notifications.business_daily import (
    DIMENSION, SOURCE, business_daily_card, business_daily_context,
)
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot, Store


DAY = date(2026, 11, 16)
PRIOR = date(2026, 11, 15)
CURRENT = {'volume': 25, 'netAmount': 4089.75, 'platformIncome': 3208.16, 'grossProfit': 387.22,
           'grossRate': .1218, 'totalAdsCostRate': -.2158, 'totalRefundsRate': -.0816}
PREVIOUS = {'volume': 20, 'netAmount': 3524.80, 'platformIncome': 4595.59, 'grossProfit': 795.29,
            'grossRate': .1743, 'totalAdsCostRate': -.2173, 'totalRefundsRate': -.0663}


def payload(current=None, previous=None, currency='USD'):
    return {'code': 1, 'data': {'code': 200, 'data': {
        'currencyCode': currency, 'current': deepcopy(CURRENT if current is None else current),
        'chainCompare': deepcopy(PREVIOUS if previous is None else previous),
        'chainCompareStartDate': PRIOR.isoformat(), 'chainCompareEndDate': PRIOR.isoformat(),
    }}}


def body(response):
    return response['data']['data']


def facts(db):
    return list(db.scalars(select(DailyMetric).where(DailyMetric.dimension == DIMENSION)
                          .order_by(DailyMetric.metric_date, DailyMetric.metric_name)))


def rows(value):
    return [e for e in value['body']['elements'] if e['tag'] == 'table'][0]['rows']


@pytest.fixture
def warehouse():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(sid='101', name='CAMPX', status=1, currency_code='USD',
                      timezone='America/Los_Angeles')
        db.add(store)
        db.commit()
        yield db, store
    engine.dispose()


def test_official_seven_business_rows_match_requested_format_and_percentage_point_changes(warehouse):
    db, store = warehouse
    response = payload()
    dashboard.persist_business_cards(db, store.sid, DAY, response)
    value = business_daily_card(db, store, DAY)
    assert value['header']['title']['content'] == 'CAMPX 经营日报'
    assert value['body']['elements'][0]['content'] == '2026年11月16日｜美元 USD'
    assert [(r['c0'], r['c1'], r['c2'], r['c3']) for r in rows(value)] == [
        ('销量', '25', '20', '+25.00%'),
        ('净销售额', '$4,089.75', '$3,524.80', '+16.03%'),
        ('平台收入', '$3,208.16', '$4,595.59', '-30.19%'),
        ('毛利润', '$387.22', '$795.29', '-51.31%'),
        ('毛利率', '12.18%', '17.43%', '-5.25 个百分点'),
        ('广告费占比¹', '21.58%', '21.73%', '-0.15 个百分点'),
        ('退款率¹', '8.16%', '6.63%', '+1.53 个百分点'),
    ]
    # Signed deductions and source-provided margins must survive presentation.
    stored = {(r.metric_date, r.metric_name): r.value for r in facts(db)}
    assert stored[DAY, 'totalAdsCostRate'] == -.2158 and stored[DAY, 'totalRefundsRate'] == -.0816
    assert stored[DAY, 'grossRate'] == .1218  # Not a guessed grossProfit / platformIncome ratio.
    assert db.scalar(select(RawSnapshot)).payload['response'] == response


def test_recollection_replaces_exact_metrics_without_accumulation_and_keeps_raw_versions(warehouse):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    updated = payload({**CURRENT, 'netAmount': 4100.00})
    dashboard.persist_business_cards(db, store.sid, DAY, updated)
    assert len(facts(db)) == 14
    assert db.scalar(select(func.count(RawSnapshot.id))) == 2
    assert db.scalar(select(func.count(DataCoverage.id))) == 2
    context = business_daily_context(db, store.sid, DAY)
    net = next(m for m in context['metrics'] if m['key'] == 'netAmount')
    assert net['current'] == 4100.00
    assert {r.source for r in facts(db)} == {SOURCE}


def test_chain_comparison_is_saved_only_for_explicitly_confirmed_adjacent_day(warehouse):
    db, store = warehouse
    response = payload()
    body(response)['chainCompareStartDate'] = '2026-11-14'
    dashboard.persist_business_cards(db, store.sid, DAY, response)
    assert {r.metric_date for r in facts(db)} == {DAY}
    assert business_daily_context(db, store.sid, DAY)['previous_date'] == PRIOR.isoformat()
    # The absence of an adjacent observation is not evidence of another currency.
    assert all(r['c2'] == '—' and r['c3'] == '—' for r in rows(business_daily_card(db, store, DAY)))


@pytest.mark.parametrize('currency', [None, '', 'usd', 'US', 'UNKNOWN'])
def test_unconfirmed_currency_rejects_partition_without_overwriting_previous_evidence(warehouse, currency):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    with pytest.raises(dashboard.BusinessDashboardError, match='currency'):
        dashboard.persist_business_cards(db, store.sid, DAY, payload(currency=currency))
    assert len(facts(db)) == 14 and db.scalar(select(func.count(RawSnapshot.id))) == 1


def test_comparison_with_explicit_different_currency_is_not_computed(warehouse):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    for fact in facts(db):
        if fact.metric_date == PRIOR:
            fact.currency = 'CAD'
    db.commit()
    result = rows(business_daily_card(db, store, DAY))
    net = next(r for r in result if r['c0'] == '净销售额')
    assert net['c1'] == '$4,089.75' and net['c2'] == 'CAD 3,524.80' and net['c3'] == '币种不同'
    assert next(r for r in result if r['c0'] == '销量')['c3'] == '+25.00%'


@pytest.mark.parametrize('now,previous,expected', [(10, 0, '前值为零'), (0, 0, '+0.00%'), (0, 10, '-100.00%')])
def test_zero_denominator_is_not_invented_as_percent_change(warehouse, now, previous, expected):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload({'volume': now}, {'volume': previous}))
    result = rows(business_daily_card(db, store, DAY))
    assert len(result) == 1 and result[0]['c3'] == expected
    assert result[0]['c1'] == str(now) and result[0]['c2'] == str(previous)


def test_complete_empty_payload_stays_no_data_without_fake_zero_or_empty_report(warehouse):
    db, store = warehouse
    result = dashboard.persist_business_cards(db, store.sid, DAY, payload({}, {}))
    assert result['metrics'] == 0 and facts(db) == []
    assert business_daily_context(db, store.sid, DAY)['availability'] == 'MISSING'
    assert business_daily_card(db, store, DAY) is None
    coverage = list(db.scalars(select(DataCoverage)))
    assert len(coverage) == 2 and all(r.status == 'COMPLETE' and r.row_count == 0 for r in coverage)
    assert db.scalar(select(func.count(RawSnapshot.id))) == 1


@pytest.mark.parametrize('field,value', [
    ('volume', -1), ('volume', 1.5), ('volume', True), ('netAmount', float('inf')),
    ('grossProfit', 'NaN'), ('grossRate', 'bad'), ('totalAdsCostRate', []), ('totalRefundsRate', False),
])
def test_bad_numbers_do_not_overwrite_an_existing_complete_partition(warehouse, field, value):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    with pytest.raises(dashboard.BusinessDashboardError, match='numeric'):
        dashboard.persist_business_cards(db, store.sid, DAY, payload({**CURRENT, field: value}))
    assert len(facts(db)) == 14
    assert next(r for r in facts(db) if r.metric_date == DAY and r.metric_name == 'volume').value == 25


@pytest.mark.parametrize('response', [
    {'code': 0, 'data': {}}, {'success': False, 'current': {}}, {'code': 401, 'data': {}},
    {'code': 1}, {'code': 1, 'data': []}, {'code': 1, 'data': {'code': 500, 'data': {'current': {}}}},
])
def test_failed_or_incomplete_responses_do_not_create_coverage_or_replace_facts(warehouse, response):
    db, store = warehouse
    with pytest.raises(dashboard.BusinessDashboardError):
        dashboard.persist_business_cards(db, store.sid, DAY, response)
    assert facts(db) == [] and db.scalar(select(func.count(DataCoverage.id))) == 0


def test_regular_sales_are_not_renamed_as_net_sales_or_used_to_fabricate_profit(warehouse):
    db, store = warehouse
    db.add(DailyMetric(store_id=store.sid, entity_id=store.sid, metric_date=DAY,
        dimension='store', metric_name='sales', value=999, currency='USD'))
    db.add(DataCoverage(store_id=store.sid, metric_date=DAY, dimension='store', row_count=1))
    db.commit()
    assert business_daily_card(db, store, DAY) is None
    dashboard.persist_business_cards(db, store.sid, DAY, payload({'volume': 25}, {}))
    assert [r['c0'] for r in rows(business_daily_card(db, store, DAY))] == ['销量']
    assert next(m for m in business_daily_context(db, store.sid, DAY)['metrics'] if m['key'] == 'netAmount')['current'] is None


def test_incomplete_coverage_and_failed_authorization_cannot_publish_business_as_complete(warehouse):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    part = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY))
    part.status = 'FAILED'
    db.commit()
    assert business_daily_card(db, store, DAY) is None
    part.status = 'COMPLETE'
    store.status = 2
    db.commit()
    assert business_daily_card(db, store, DAY) is None


def test_exact_day_observations_with_warning_keep_warning_and_suppress_business_judgments(warehouse):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    value = business_daily_card(db, store, DAY, data_warning='目标日采集失败，展示该日已归档观测')
    assert all(r['c3'] == '待确认' for r in rows(value))
    assert rows(value)[1]['c1'] == '$4,089.75'
    assert '目标日采集失败' in json.dumps(value, ensure_ascii=False)


def test_missing_private_credentials_keeps_optional_collection_unconfigured(warehouse, tmp_path):
    db, _ = warehouse
    settings = SimpleNamespace(lingxing_business_credentials_file=str(tmp_path / 'absent.json'))
    assert dashboard.collect_business_for_day(db, settings, {'data': []}, DAY.isoformat()) == {
        'status': 'NOT_CONFIGURED', 'partitions': [], 'metrics': 0,
    }


def fake_mcp(monkeypatch, tmp_path, *, help_kind='read', schema_kind='read', missing_schema=False,
             action_response=None, inventory_help_kind='read', inventory_schema_kind='read',
             inventory_response=None, inventory_properties=None):
    calls = []
    credentials = tmp_path / 'business-test-private.json'
    credentials.write_text(json.dumps({'url': 'https://openmcp.lingxing.com/mcp-servers/lingxing-mcp',
                                      'key': 'synthetic-mcp-test-credential'}), encoding='utf-8')
    credentials.chmod(0o600)
    settings = SimpleNamespace(lingxing_business_credentials_file=str(credentials),
                               lingxing_sources_file=str(tmp_path / 'sources-not-configured.json'))

    async def unpaced(session, tool, params):
        return await session.call_tool(tool, params)

    monkeypatch.setattr(dashboard, 'paced_call', unpaced)

    class Http:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    @asynccontextmanager
    async def stream(url, **kwargs):
        assert url == 'https://openmcp.lingxing.com/mcp-servers/lingxing-mcp'
        yield None, None, None

    class McpSession:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=x) for x in ('help', 'search', 'action')])

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            tool_id = arguments.get('toolId') or arguments.get('query')
            inventory = tool_id == dashboard.STORE_TOOL
            if name == 'help':
                kind = inventory_help_kind if inventory else help_kind
                value = {'code': 1, 'data': {'tools': [{'toolId': tool_id, 'toolType': kind}]}}
            elif name == 'search':
                properties = ({} if inventory_properties is None else inventory_properties) if inventory else {
                    key: {} for key in ('startDate', 'endDate', 'currencyCode', 'sids', 'aggregateType', 'indexName')}
                if missing_schema and not inventory:
                    del properties['sids']
                kind = inventory_schema_kind if inventory else schema_kind
                value = {'code': 1, 'data': {'toolId': tool_id, 'toolType': kind,
                                           'inputSchema': {'type': 'object', 'properties': properties}}}
            elif inventory:
                value = inventory_response if inventory_response is not None else {
                    'code': 1, 'data': {'code': 1, 'list': [{'id': 101}]}}
            else:
                value = action_response if action_response is not None else payload()
                if callable(value):
                    value = value(arguments['params'])
            return SimpleNamespace(isError=False, structuredContent=value, content=[])

    monkeypatch.setattr(dashboard.httpx, 'AsyncClient', Http)
    monkeypatch.setattr(dashboard, 'streamable_http_client', stream)
    monkeypatch.setattr(dashboard, 'ClientSession', McpSession)
    monkeypatch.setattr(dashboard, 'collection_states', lambda settings, sids: {sid: True for sid in sids})

    def run_mocked(coroutine):
        # This contract test has fully in-memory async contexts and no I/O.
        # Drive that coroutine directly so discovery gates cannot accidentally
        # open a real transport/event loop while using the fake MCP session.
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        finally:
            coroutine.close()
        raise AssertionError('The fake MCP transport unexpectedly performed I/O')

    monkeypatch.setattr(dashboard.asyncio, 'run', run_mocked)
    return settings, calls


@pytest.mark.parametrize('help_kind,schema_kind', [('write', 'read'), ('read', 'write'), ('unknown', 'read')])
def test_discovery_and_schema_must_both_confirm_fixed_read_only_tool_before_action(
        warehouse, tmp_path, monkeypatch, help_kind, schema_kind):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path, help_kind=help_kind, schema_kind=schema_kind)
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, DAY.isoformat())
    assert result['status'] == 'PARTIAL_FAILED'
    assert not any(name == 'action' and args['toolId'] == dashboard.TOOL for name, args in calls)
    assert facts(db) == []


def test_schema_drift_blocks_action_without_guessing_new_parameter_shapes(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path, missing_schema=True)
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, DAY.isoformat())
    assert result['status'] == 'PARTIAL_FAILED'
    assert [name for name, _ in calls] == ['help', 'search', 'action', 'help', 'search']
    assert not any(name == 'action' and args['toolId'] == dashboard.TOOL for name, args in calls)


def test_collect_calls_only_the_confirmed_tool_with_exact_day_and_requested_fields(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path)
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}, {'sid': '999', 'status': 2}]}, DAY.isoformat())
    assert result['status'] == 'COMPLETE' and result['metrics'] == 14
    assert [name for name, _ in calls] == ['help', 'search', 'action', 'help', 'search', 'action']
    assert calls[2][1] == {'toolId': dashboard.STORE_TOOL, 'params': {}}
    assert calls[-1][1] == {'toolId': dashboard.TOOL, 'params': {
        'startDate': DAY.isoformat(), 'endDate': DAY.isoformat(), 'currencyCode': 'USD',
        'sids': [101], 'aggregateType': '2', 'indexName': dashboard.FIELDS,
    }}


def test_store_collection_switch_is_rechecked_before_each_business_request(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path)
    states = iter([True, False])
    monkeypatch.setattr(dashboard, 'collection_states', lambda settings, sids: {store.sid: next(states)})
    result = asyncio.run(dashboard._collect(db, settings, [{'sid': store.sid}], {store.sid: [DAY.isoformat()]}))
    assert result['metrics'] == 0
    assert not any(name == 'action' and args['toolId'] == dashboard.TOOL for name, args in calls)


def test_action_failure_leaves_existing_partition_intact_and_reports_a_sanitized_type(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    settings, _ = fake_mcp(monkeypatch, tmp_path, action_response={'code': 401, 'message': 'sensitive upstream text'})
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, DAY.isoformat())
    assert result['status'] == 'PARTIAL_FAILED' and result['partitions'] == []
    assert result['failures'] == [{'store_id': store.sid, 'report_date': DAY.isoformat(),
                                  'error_type': 'BusinessDashboardError'}]
    assert len(facts(db)) == 14 and 'sensitive' not in json.dumps(result)
    assert db.scalar(select(func.count(RawSnapshot.id))) == 1
    context = business_daily_context(db, store.sid, DAY)
    assert context['availability'] == 'FAILED'
    assert all(item['current'] is None and item['change'] is None for item in context['metrics'])
    assert next(item for item in context['metrics'] if item['key'] == 'netAmount')['previous'] == 3524.80
    assert business_daily_card(db, store, DAY) is None


def test_successful_retry_restores_failed_business_partition_without_summing_old_values(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    settings, _ = fake_mcp(monkeypatch, tmp_path, action_response={'code': 500})
    stores = {'data': [{'sid': store.sid, 'status': 1}]}
    assert dashboard.collect_business_for_day(db, settings, stores, DAY.isoformat())['status'] == 'PARTIAL_FAILED'
    assert business_daily_context(db, store.sid, DAY)['availability'] == 'FAILED'
    settings, _ = fake_mcp(monkeypatch, tmp_path, action_response=payload({**CURRENT, 'netAmount': 4200}))
    result = dashboard.collect_business_for_day(db, settings, stores, DAY.isoformat())
    assert result['status'] == 'COMPLETE' and result['metrics'] == 14
    context = business_daily_context(db, store.sid, DAY)
    assert context['availability'] == 'AVAILABLE'
    net = next(item for item in context['metrics'] if item['key'] == 'netAmount')
    assert net['current'] == 4200 and net['previous'] == 3524.80 and net['change'] is not None
    assert business_daily_card(db, store, DAY) is not None
    assert len(facts(db)) == 14 and db.scalar(select(func.count(RawSnapshot.id))) == 2
    assert db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY)).status == 'COMPLETE'


def test_discovery_failure_marks_all_requested_dates_failed_but_preserves_other_sources_and_raw(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    db.add(DailyMetric(store_id=store.sid, metric_date=DAY, dimension='store', entity_id=store.sid,
        metric_name='sales', value=777, currency='USD'))
    db.add(DataCoverage(store_id=store.sid, metric_date=DAY, dimension='store', status='COMPLETE', row_count=1))
    db.commit()
    settings, calls = fake_mcp(monkeypatch, tmp_path, schema_kind='write')
    dates = [DAY.isoformat(), '2026-11-17']
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, {store.sid: dates})
    assert result['status'] == 'PARTIAL_FAILED'
    assert [name for name, _ in calls] == ['help', 'search', 'action', 'help', 'search']
    assert len(facts(db)) == 14 and db.scalar(select(func.count(RawSnapshot.id))) == 1
    for day in dates:
        part = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == date.fromisoformat(day),
            DataCoverage.dimension == DIMENSION))
        assert part.status == 'FAILED'
        assert business_daily_context(db, store.sid, day)['availability'] == 'FAILED'
    assert db.scalar(select(DataCoverage).where(DataCoverage.metric_date == PRIOR)).status == 'COMPLETE'
    assert db.scalar(select(DataCoverage).where(DataCoverage.dimension == 'store')).status == 'COMPLETE'
    assert db.scalar(select(DailyMetric).where(DailyMetric.dimension == 'store')).value == 777


def test_one_requested_day_failure_does_not_stop_other_days_and_only_hides_failed_partition(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())

    def respond(params):
        return {'code': 500} if params['startDate'] == DAY.isoformat() else payload()

    settings, calls = fake_mcp(monkeypatch, tmp_path, action_response=respond)
    result = dashboard.collect_business_for_day(db, settings, {'data': [{'sid': store.sid, 'status': 1}]},
        {store.sid: [DAY.isoformat(), '2026-11-14']})
    assert result['status'] == 'PARTIAL_FAILED' and result['metrics'] == 7
    assert [params['params']['startDate'] for name, params in calls
            if name == 'action' and params['toolId'] == dashboard.TOOL] == [
        DAY.isoformat(), '2026-11-14',
    ]
    assert result['failures'] == [{'store_id': store.sid, 'report_date': DAY.isoformat(),
                                  'error_type': 'BusinessDashboardError'}]
    assert result['partitions'][0]['dates'] == ['2026-11-14']
    assert business_daily_context(db, store.sid, DAY)['availability'] == 'FAILED'
    assert business_daily_context(db, store.sid, '2026-11-14')['availability'] == 'AVAILABLE'
    assert len(facts(db)) == 21 and db.scalar(select(func.count(RawSnapshot.id))) == 2


def test_optional_source_bad_date_failure_is_isolated_from_main_collection(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path)
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, {store.sid: 'not-a-date'})
    assert result['status'] == 'PARTIAL_FAILED' and result['metrics'] == 0 and calls == []


@pytest.mark.parametrize('result', [
    SimpleNamespace(isError=True, structuredContent={'current': {}}, content=[]),
    SimpleNamespace(isError=False, structuredContent=None, content=[]),
    SimpleNamespace(isError=False, structuredContent=None, content=[SimpleNamespace(type='text', text='invalid-json')]),
])
def test_mcp_error_or_unparseable_document_is_not_a_business_result(result):
    with pytest.raises(dashboard.BusinessDashboardError):
        dashboard._document(result)


def test_single_json_text_document_is_supported():
    response = payload()
    result = SimpleNamespace(isError=False, structuredContent=None,
        content=[SimpleNamespace(type='text', text=json.dumps(response))])
    assert dashboard._document(result) == response


@pytest.mark.parametrize('listing,error_type', [
    ([], 'BusinessStoreNotVisibleError'),
    ([{'id': 202}], 'BusinessStoreNotVisibleError'),
    ([{'id': 101}, {'id': 101}], 'BusinessStoreInventoryError'),
    ([{'id': 0}], 'BusinessStoreInventoryError'),
    ([{'id': -1}], 'BusinessStoreInventoryError'),
    ([{'id': True}], 'BusinessStoreInventoryError'),
    ([{'id': '101'}], 'BusinessStoreInventoryError'),
    ([{'sid': 101}], 'BusinessStoreInventoryError'),
    ({'id': 101}, 'BusinessStoreInventoryError'),
])
def test_empty_invisible_or_malformed_inventory_never_requests_cards_or_erases_old_facts(
        warehouse, tmp_path, monkeypatch, listing, error_type):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    inventory = {'code': 1, 'data': {'code': 1, 'list': listing}}
    settings, calls = fake_mcp(monkeypatch, tmp_path, inventory_response=inventory)
    dates = [DAY.isoformat(), '2026-11-17']
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, {store.sid: dates})
    assert result['status'] == 'PARTIAL_FAILED' and result['metrics'] == 0
    assert result['failures'] == [{'store_id': store.sid, 'report_date': day, 'error_type': error_type} for day in dates]
    assert [args['toolId'] for name, args in calls if name == 'action'] == [dashboard.STORE_TOOL]
    assert len(facts(db)) == 14 and db.scalar(select(func.count(RawSnapshot.id))) == 1
    assert all(business_daily_context(db, store.sid, day)['availability'] == 'FAILED' for day in dates)


@pytest.mark.parametrize('help_kind,schema_kind', [('write', 'read'), ('read', 'write')])
def test_store_inventory_tool_must_also_be_confirmed_read_only_before_any_action(
        warehouse, tmp_path, monkeypatch, help_kind, schema_kind):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path,
        inventory_help_kind=help_kind, inventory_schema_kind=schema_kind)
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, DAY.isoformat())
    assert result['status'] == 'PARTIAL_FAILED' and not any(name == 'action' for name, _ in calls)
    assert facts(db) == []


def test_store_inventory_schema_cannot_gain_unverified_arguments(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path, inventory_properties={'sids': {'type': 'array'}})
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, DAY.isoformat())
    assert result['status'] == 'PARTIAL_FAILED' and not any(name == 'action' for name, _ in calls)


def test_only_inventory_visible_stores_reach_the_business_cards_tool(warehouse, tmp_path, monkeypatch):
    db, store = warehouse
    dashboard.persist_business_cards(db, store.sid, DAY, payload())
    inventory = {'code': 1, 'success': True, 'data': {'code': 1, 'list': [
        {'id': 202, 'name': '另一家可见店铺', 'country_code': 'US', 'is_concept': 0},
    ]}}
    settings, calls = fake_mcp(monkeypatch, tmp_path, inventory_response=inventory)
    result = dashboard.collect_business_for_day(db, settings, {'data': [
        {'sid': store.sid, 'status': 1}, {'sid': '202', 'status': 1},
    ]}, DAY.isoformat())
    assert result['status'] == 'PARTIAL_FAILED' and result['metrics'] == 14
    assert result['failures'] == [{'store_id': '101', 'report_date': DAY.isoformat(),
                                  'error_type': 'BusinessStoreNotVisibleError'}]
    actions = [args for name, args in calls if name == 'action']
    assert actions[0] == {'toolId': dashboard.STORE_TOOL, 'params': {}}
    assert [args['params']['sids'] for args in actions if args['toolId'] == dashboard.TOOL] == [[202]]
    assert business_daily_context(db, '101', DAY)['availability'] == 'FAILED'
    assert business_daily_context(db, '202', DAY)['availability'] == 'AVAILABLE'
    assert len([r for r in facts(db) if r.store_id == '101']) == 14


@pytest.mark.parametrize('response', [
    {'code': 0, 'data': {'code': 1, 'list': [{'id': 101}]}},
    {'code': 1, 'success': False, 'data': {'code': 1, 'list': [{'id': 101}]}},
    {'code': 1, 'data': [{'id': 101}]},
    {'code': 1, 'data': {'list': [{'id': 101}]}},
])
def test_unconfirmed_store_inventory_response_does_not_reuse_arbitrary_record_shapes(
        warehouse, tmp_path, monkeypatch, response):
    db, store = warehouse
    settings, calls = fake_mcp(monkeypatch, tmp_path, inventory_response=response)
    result = dashboard.collect_business_for_day(db, settings,
        {'data': [{'sid': store.sid, 'status': 1}]}, DAY.isoformat())
    assert result['failures'][0]['error_type'] == 'BusinessStoreInventoryError'
    assert [args['toolId'] for name, args in calls if name == 'action'] == [dashboard.STORE_TOOL]
    assert facts(db) == []


def test_source_reader_allowlist_rejects_any_other_tool_before_discovery(tmp_path, monkeypatch):
    fake_mcp(monkeypatch, tmp_path)
    with pytest.raises(dashboard.BusinessDashboardError, match='allowlist'):
        asyncio.run(dashboard._read_schema(SimpleNamespace(), 'campaign_update'))
