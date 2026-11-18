"""Only scheduled, verified business pages extend the quiet morning digest."""
from datetime import date, datetime, timezone
from types import ModuleType, SimpleNamespace
import sys

import pytest

from jx_alert.notifications import morning_digest
from jx_alert.notifications.cards import card
from jx_alert.notifications.quiet_policy import quiet_blocks


DAY = date(2026, 11, 16)


@pytest.fixture
def renderer(monkeypatch):
    calls = []
    stores = [SimpleNamespace(sid='one', name='CAMPX'), SimpleNamespace(sid='two', name='Geteen')]

    def business(db, store, day, *, settings=None, data_warning=None, reporting_day=None):
        assert reporting_day == date(2026, 11, 16)
        calls.append((store.sid, day, data_warning))
        # The business renderer owns field/currency/coverage verification. This
        # test isolates its None contract from morning routing and ordering.
        return (card(f'{store.name} 经营日报｜{day}', subtitle='美元 USD',
            sections=[('经营日报', ['指标', '当日', '前一日', '日环比'], [('销量', '25', '20', '+25.00%')])])
            if store.sid == 'one' and data_warning is None else None)

    module = ModuleType('jx_alert.notifications.business_daily')
    module.business_daily_card = business
    monkeypatch.setitem(sys.modules, 'jx_alert.notifications.business_daily', module)
    monkeypatch.setattr(morning_digest, '_health_chunks', lambda *args, **kwargs: [
        (card('店铺绩效与违规快报 · 1', subtitle='', sections=[]), ['one']),
        (card('店铺绩效与违规快报 · 2', subtitle='', sections=[]), ['two']),
    ])
    finding = {'report_date': DAY.isoformat(), 'mature_date': '2026-11-09', 'spend': 100,
        'currency': 'USD', 'mature_spend': 100, 'mature_currency': 'USD', 'mature_orders': 0,
        'campaign_name': '已核实活动', 'state': '投放中', 'topics': [('花费无订单', '2026-11-09')], 'kpis': {}}
    monkeypatch.setattr(morning_digest, 'sp_campaign_exceptions',
        lambda db, settings, store, day: ([finding] if store.sid == 'two' else [], None))
    return stores, calls


def render(stores, **kwargs):
    return morning_digest.morning_cards(None, SimpleNamespace(), stores, {s.sid: DAY for s in stores},
        send_day=date(2026, 11, 17), now=datetime(2026, 11, 17, tzinfo=timezone.utc), **kwargs)


def test_all_health_precedes_verified_business_then_ad_exceptions_and_no_empty_business_page(renderer):
    stores, calls = renderer
    pages = render(stores)
    assert [value['header']['title']['content'] for value, _ in pages] == [
        '店铺绩效与违规快报 · 1', '店铺绩效与违规快报 · 2',
        'CAMPX 经营日报｜2026-11-16', '商品推广活动待复核',
    ]
    assert pages[2][1] == ['one'] and pages[3][1] == ['two']
    assert calls == [('one', DAY, None), ('two', DAY, None)]


def test_failed_target_warning_reaches_business_renderer_without_reusing_stale_values(renderer):
    stores, calls = renderer
    pages = render(stores, warnings={'one': '目标日采集失败'})
    assert not any('经营日报' in value['header']['title']['content'] for value, _ in pages)
    assert calls[0] == ('one', DAY, '目标日采集失败')


def test_explicit_advertising_question_can_omit_business_reports(renderer):
    stores, calls = renderer
    pages = render(stores, include_business=False)
    assert calls == [] and len(pages) == 3
    assert pages[-1][0]['header']['title']['content'] == '商品推广活动待复核'


def test_history_display_keeps_exact_actual_and_target_dates_without_marking_valid_history_failed(renderer):
    stores, calls = renderer
    target = date(2026, 11, 17)
    pages = render(stores, target_dates={s.sid: target for s in stores})
    business = pages[2][0]
    assert business['header']['title']['content'] == 'CAMPX 经营日报｜2026-11-16'
    assert business['body']['elements'][-1]['content'] == '目标报表日 2026-11-17；展示历史报表日 2026-11-16。'
    assert calls[0] == ('one', DAY, None)


def test_quiet_policy_allows_only_scheduled_business_digest_and_keeps_manual_push_blocked():
    settings = SimpleNamespace(feishu_quiet_delivery_enabled=True)
    payload = {'morning_message_kind': 'business_daily'}
    assert not quiet_blocks(settings, 'scheduled-daily:morning:business:one', payload)
    assert quiet_blocks(settings, 'chat-report:manual-business:one', payload)
    assert quiet_blocks(settings, 'scheduled-daily:morning:quality:one', {'morning_message_kind': 'data_quality'})


def test_scheduled_outbox_marks_business_kind_and_reuses_the_whole_frozen_digest(renderer):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from jx_alert.notifications.outbox import NotificationOutbox
    from jx_alert.notifications.worker import _prior_morning_page
    from jx_alert.storage.db import Base
    from jx_alert.storage.models import Store

    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(feishu_quiet_delivery_enabled=True)
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    with Session(engine) as db:
        stores = [Store(sid='one', name='CAMPX', status=1, timezone='America/Los_Angeles'),
                  Store(sid='two', name='Geteen', status=1, timezone='America/Los_Angeles')]
        db.add_all(stores)
        db.flush()
        kwargs = dict(send_day=now.date(), due=now, delivery={'receive_id': 'oc_reports'}, now=now)
        dates = {s.sid: DAY for s in stores}
        first = morning_digest.enqueue_morning_digest(db, settings, stores, dates, {}, **kwargs)
        rows = list(db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
        assert [r.payload['morning_message_kind'] for r in rows] == [
            'health', 'health', 'business_daily',
        ]
        assert all(not quiet_blocks(settings, r.idempotency_key, r.payload) for r in rows)
        assert rows[2].payload['store_report_dates'] == {'one': {'target': str(DAY), 'actual': str(DAY)}}
        assert not _prior_morning_page(db, rows[0].payload, rows[0].id)
        rows[0].status = 'DELIVERED'
        rows[1].status = 'RETRY'
        db.flush()
        assert _prior_morning_page(db, rows[2].payload, rows[2].id)
        rows[1].status = 'DELIVERED'
        db.flush()
        assert not _prior_morning_page(db, rows[2].payload, rows[2].id)
        again = morning_digest.enqueue_morning_digest(db, settings, stores, dates, {}, **kwargs)
        assert first['outbox_ids'] == again['outbox_ids']
    engine.dispose()
