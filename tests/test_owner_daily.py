"""Source and freeze tests for exact-day owner reports."""
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.lingxing.api_business import FINANCE, PRODUCT, SOURCE
from jx_alert.notifications.morning_digest import enqueue_morning_digest
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.owner_daily import owner_daily_rows
from jx_alert.notifications.quiet_policy import quiet_blocks
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store


DAY = date(2026, 11, 20)
CLOCK = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)
LOCAL = ZoneInfo('Asia/Shanghai')
PST = 'America/Los_Angeles'


def _store(db, sid='5174', zone=PST):
    row = Store(sid=sid, name='测试店铺' + sid, timezone=zone, status=1)
    db.add(row)
    return row


def _part(db, sid, dim, rows, *, day=DAY, at=CLOCK - timedelta(minutes=5), currency='USD'):
    db.add(DataCoverage(store_id=sid, metric_date=day, dimension=dim, row_count=len(rows),
        completed_at=at.replace(tzinfo=None)))
    for entity, metrics in rows.items():
        for key, value in metrics.items():
            db.add(DailyMetric(store_id=sid, metric_date=day, dimension=dim, entity_id=entity,
                source=SOURCE, metric_name=key, value=value, currency=currency))
    db.flush()


def test_complete_exact_orders_and_settled_refunds_stay_separate_from_ads_grain():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = _store(db)
        _part(db, store.sid, PRODUCT, {'ASIN1': {'adSpend': 31, 'volume': 9,
                                               'returnGoodsCount': 0, 'netAmount': 100}})
        _part(db, store.sid, FINANCE, {store.sid: {'totalSalesRefunds': -14.5}})
        (result,), provisional = owner_daily_rows(db, [store], DAY, now=CLOCK)
        assert provisional is False
        assert result['ad_spend'] == 31 and {'SP', 'SB', 'SD'} <= set(result['covered_ad_types'])
        assert (result['sales'], result['return_units'], result['sold_units'], result['refund_amount']) == (100, 0, 9, 14.5)
        assert result['currency'] == 'USD'
        (wrong_date,), _ = owner_daily_rows(db, [store], DAY - timedelta(days=1), now=CLOCK)
        assert all(wrong_date[field] is None for field in ('sales', 'ad_spend', 'refund_amount'))


def test_today_needs_current_date_and_fresh_capture_and_is_provisional():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    day = CLOCK.astimezone(ZoneInfo(PST)).date()
    with Session(engine) as db:
        fresh, stale, unknown = _store(db, '1'), _store(db, '2'), _store(db, '3', None)
        _part(db, fresh.sid, PRODUCT, {'ASIN1': {'adSpend': 0, 'netAmount': 0}}, day=day)
        _part(db, stale.sid, PRODUCT, {'ASIN2': {'adSpend': 1, 'netAmount': 7}}, day=day,
              at=CLOCK - timedelta(hours=2))
        _part(db, unknown.sid, PRODUCT, {'ASIN3': {'adSpend': 22, 'netAmount': 4}}, day=day)
        results, provisional = owner_daily_rows(db, [fresh, stale, unknown], day, now=CLOCK)
        assert provisional is True
        assert [r['ad_spend'] for r in results] == [0, None, None]
        assert [r['sales'] for r in results] == [0, None, None]
        assert all(r['return_units'] is None and r['refund_amount'] is None for r in results)


def test_preclose_capture_and_mixed_currency_do_not_become_completed_totals():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        early, mixed = _store(db, '1'), _store(db, '2')
        _part(db, early.sid, PRODUCT, {'ASIN1': {'adSpend': 20}},
              at=datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc))  # still Nov 20 in LA
        _part(db, mixed.sid, PRODUCT, {'ASIN2': {'adSpend': 22, 'netAmount': 210}})
        _part(db, mixed.sid, FINANCE, {mixed.sid: {'totalSalesRefunds': 33}}, currency='CAD')
        (r1, r2), _ = owner_daily_rows(db, [early, mixed], DAY, now=CLOCK)
        assert r1['ad_spend'] is None
        assert r2['sales'] == 210 and r2['refund_amount'] is None


def test_scheduled_opt_in_freezes_owner_pages_without_duplicate_delivery(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(feishu_owner_daily_report_enabled=True, feishu_quiet_delivery_enabled=True,
        feishu_routing_file=str(tmp_path / 'routing.json'), lingxing_performance_observation_only=True)
    delivery = {'receive_id': 'oc_demo', 'receive_id_type': 'chat_id', 'role': 'daily_snapshot'}
    with Session(engine) as db:
        first, second = _store(db, '1'), _store(db, '2')
        _part(db, first.sid, PRODUCT, {'ASIN1': {'adSpend': 20, 'netAmount': 100,
                                                'volume': 4, 'returnGoodsCount': 1}})
        _part(db, second.sid, PRODUCT, {'ASIN2': {'adSpend': 0, 'netAmount': 0}})
        kwargs = dict(send_day=CLOCK.astimezone(LOCAL).date(), due=CLOCK, delivery=delivery, now=CLOCK)
        dates = {s.sid: DAY for s in (first, second)}
        outcome = enqueue_morning_digest(db, settings, [first, second], dates, {}, **kwargs)
        assert outcome['outbox_ids']
        rows = list(db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
        assert rows[0].payload['morning_message_kind'] == 'owner_daily'
        assert rows[0].payload['store_ids'] == [first.sid, second.sid]
        assert '2026-11-20' in json.dumps(rows[0].payload['card'], ensure_ascii=False)
        assert all(row.payload['morning_message_kind'] != 'business_daily' for row in rows)
        assert not quiet_blocks(settings, rows[0].idempotency_key, rows[0].payload)
        again = enqueue_morning_digest(db, settings, [first, second], dates, {}, **kwargs)
        assert outcome == again

def test_owner_page_remaps_mentions_without_sending_and_blocks_group_change(tmp_path):
    from jx_alert.notifications.cards import card
    from jx_alert.notifications.owner_daily import owner_daily_delivery_card
    from jx_alert.notifications.routing import report_group_delivery

    path = tmp_path / 'routing.json'
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'store_ids': [1, 2]}))
    settings = SimpleNamespace(feishu_owner_daily_report_enabled=True, feishu_transport='app',
        feishu_daily_report_chat_id='reports', feishu_routing_file=str(path), collection_scope_file=str(scope))
    routing = {'default_chat_id': 'reports', 'groups': {'reports': {'chat_id': 'oc_reports',
        'verified_member_open_ids': ['ou_current']}},
        'stores': {'1': {'open_ids': ['ou_current', 'ou_removed']}, '2': {'open_ids': ['ou_current']}}}
    path.write_text(json.dumps(routing))
    destination = report_group_delivery(settings, routing, role='daily_snapshot')
    key = 'scheduled-daily:morning:owner-example'
    payload = {'store_ids': ['1', '2'], 'fixed_route': True, 'transport': 'app',
               'delivery': destination, 'card': card('经营日报', subtitle='', sections=[])}
    value = owner_daily_delivery_card(settings, key, payload)
    output = json.dumps(value, ensure_ascii=False)
    assert output.count('<at id=ou_current></at>') == 1 and 'ou_removed' not in output
    # The DB can still consider both stores active after removing one from the explicit owner scope.
    scope.write_text(json.dumps({'store_ids': [1]}))
    assert owner_daily_delivery_card(settings, key, payload) is None
    assert owner_daily_delivery_card(settings, key, {**payload, 'store_ids': ['1']}) is not None
    scope.unlink()
    assert owner_daily_delivery_card(settings, key, payload) is None
    scope.write_text('{"store_ids": "12"}')
    assert owner_daily_delivery_card(settings, key, payload) is None
    scope.write_text(json.dumps({'store_ids': [1, 2]}))
    settings.feishu_owner_daily_report_enabled = False
    assert owner_daily_delivery_card(settings, key, payload) is None
    settings.feishu_owner_daily_report_enabled = True
    routing['groups']['reports']['chat_id'] = 'oc_new'
    path.write_text(json.dumps(routing))
    assert owner_daily_delivery_card(settings, key, payload) is None
