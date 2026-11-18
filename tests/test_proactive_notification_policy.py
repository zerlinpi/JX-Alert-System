"""Only supported active ads and useful authorized daily facts are broadcast."""
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications import worker
from jx_alert.notifications.ad_repeat import INCIDENT
from jx_alert.notifications.daily_report import enqueue_daily_snapshots
from jx_alert.notifications.morning_digest import morning_cards
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.quiet_policy import quiet_blocks
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, NotificationLog, Store, StorePerformance, SyncJob


NOW = datetime(2026, 11, 18, 1, tzinfo=timezone.utc)
DAY = date(2026, 11, 16)


@pytest.fixture
def warehouse(tmp_path):
    routing = tmp_path / 'routing.json'
    routing.write_text(json.dumps({'default_chat_id': 'report', 'groups': {
        'report': {'chat_id': 'oc_report'}}}), encoding='utf-8')
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport='app',
        feishu_daily_report_enabled=True, feishu_daily_report_cron='0 9 * * *',
        feishu_daily_report_start_date='', feishu_daily_report_chat_id='report',
        feishu_timeout=1, feishu_routing_file=str(routing),
        store_controls_file=str(tmp_path / 'switches.json'), lingxing_performance_source_timezone='',
        lingxing_performance_observation_only=True, lingxing_performance_max_age_hours=8,
        feishu_quiet_delivery_enabled=False)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        store = Store(sid='5174', name='Geteen', status=1, timezone='America/Los_Angeles')
        db.add(store)
        db.add(Campaign(store_id=store.sid, campaign_id='a', campaign_type='sp', name='活动甲',
            state='enabled', updated_at=NOW.replace(tzinfo=None)))
        db.add(StorePerformance(store_id=store.sid, pull_date=DAY, ahr_score=256, ahr_status='GREAT',
            source_updated_at='2026-11-18 07:00:00', captured_at=NOW.replace(tzinfo=None),
            indicators={}, standards={}))
        db.commit()
        yield db, settings, store, sessions
    engine.dispose()


def active_payload():
    return {'store_id': '5174', 'entity_id': 'a', 'material_ad_alert': True,
        'ad_campaign_id': 'a', 'ad_campaign_type': 'sp', 'transport': 'app',
        'text': '活动甲点击花费明显上升', 'delivery': {'role': 'group',
            'receive_id': 'oc_report', 'receive_id_type': 'chat_id', 'open_ids': []}}


@pytest.mark.parametrize('text', ['3个活动缺少完整基线或成熟归因证据',
    '授权或同步状态异常，无法完成广告判断', '目标日报尚未完整采集，无法判断广告',
    '商品推广接口无记录，无法判断广告'])
def test_quality_messages_are_silent_even_when_legacy_quiet_switch_is_off(text):
    settings = SimpleNamespace(feishu_quiet_delivery_enabled=False)
    payload = {'text': text, 'card': {'body': {'elements': []}}}
    assert quiet_blocks(settings, 'scheduled-daily:old', payload)
    # The operator can explicitly ask for the status in the original conversation.
    for role in ('group_question', 'private_question'):
        asked = {**payload, 'transport': 'app', 'delivery': {
            'role': role, 'reply_to_message_id': 'om_question'}}
        assert not quiet_blocks(settings, 'group-question:om_question:0', asked)


def test_focused_producer_omits_unknown_ad_status_and_invalid_stores_without_losing_health(warehouse):
    db, settings, store, _ = warehouse
    missing = Store(sid='5175', name='无绩效店铺', status=1)
    unauthorized = Store(sid='5176', name='未授权店铺', status=2)
    db.add_all([missing, unauthorized])
    db.commit()
    for bad in (missing, unauthorized):
        assert morning_cards(db, settings, [bad], {bad.sid: DAY}, send_day=NOW.date(), now=NOW,
                             proactive=True) == []
    result = enqueue_daily_snapshots(db, settings, store_dates={s.sid: DAY for s in (store, missing, unauthorized)},
        delivery_date=NOW.date(), now=NOW, focused_morning=True)
    rows = [db.get(NotificationOutbox, key) for key in result['outbox_ids']]
    assert len(rows) == 1 and rows[0].payload['store_ids'] == ['5174']
    text = json.dumps(rows[0].payload['card'], ensure_ascii=False)
    assert '评分 256' in text
    assert all(term not in text for term in ('未授权店铺', '无绩效店铺', '完整基线', '尚未完整采集', '源时区', '待确认'))
    # Questions still have access to the real source status; silence is not zero/healthy.
    queried = morning_cards(db, settings, [store], {store.sid: DAY}, send_day=NOW.date(), now=NOW)
    assert '尚未完整采集' in json.dumps(queried, ensure_ascii=False)
    assert '领星端接口异常' not in json.dumps(queried, ensure_ascii=False)


def test_proactive_daily_ad_reuses_mature_source_proof_and_preserves_campaign_name(warehouse):
    from test_ad_review_evidence import fact, seed, snapshot, update_raw

    db, settings, store, _ = warehouse
    ad = seed(db)
    ad.state = 'paused'
    ad.name = '接口无记录测试活动'
    for metric in ('orders', 'sales', 'units'):
        fact(db, metric).value = 0
        update_raw(db, metric, 0)
    record = snapshot(db)
    captured = record.captured_at
    record.captured_at = datetime(2026, 11, 10)
    db.commit()
    cards = morning_cards(db, settings, [store], {store.sid: DAY}, send_day=NOW.date(), now=NOW, proactive=True)
    assert all(card['header']['title']['content'] != '商品推广活动待复核' for card, _ in cards)
    record.captured_at = captured
    db.commit()
    result = enqueue_daily_snapshots(db, settings, store_dates={store.sid: DAY},
        delivery_date=NOW.date(), now=NOW, focused_morning=True)
    rows = [db.get(NotificationOutbox, key) for key in result['outbox_ids']]
    ad_notice, = [row for row in rows if row.payload['morning_message_kind'] == 'ad_exception']
    assert ad_notice.payload['ad_group_owners'] is True
    assert not quiet_blocks(settings, ad_notice.idempotency_key, ad_notice.payload)
    message = json.dumps(ad_notice.payload, ensure_ascii=False)
    assert '接口无记录测试活动' in message and '已暂停' in message
    assert '成熟广告有花费但无归因订单' in message and '未知（' not in message
    reservation, = list(db.scalars(select(SyncJob).where(SyncJob.job_type == INCIDENT)))
    assert reservation.metadata_json['origin'] == 'daily_report'
    assert reservation.metadata_json['outbox_ids'] == [ad_notice.id]
    cards = morning_cards(db, settings, [store], {store.sid: DAY}, send_day=NOW.date(), now=NOW, proactive=True)
    assert all(card['header']['title']['content'] != '商品推广活动待复核' for card, _ in cards)


@pytest.mark.parametrize('age', [timedelta(hours=9), -timedelta(minutes=1)])
def test_old_or_future_health_observations_do_not_become_automatic_status_pages(warehouse, age):
    db, settings, store, _ = warehouse
    observation = db.scalar(select(StorePerformance))
    observation.captured_at = (NOW-age).replace(tzinfo=None)
    db.commit()
    assert morning_cards(db, settings, [store], {store.sid: DAY}, send_day=NOW.date(), now=NOW, proactive=True) == []


def test_unauthorized_store_has_no_legacy_scheduled_daily(warehouse):
    db, settings, store, _ = warehouse
    store.status=2
    db.commit()
    result = enqueue_daily_snapshots(db, settings, store_dates={store.sid:DAY}, delivery_date=NOW.date(), now=NOW)
    assert result['outbox_ids'] == [] and result['reason'] == 'no_authorized_stores'


@pytest.mark.parametrize('state,age,expected', [
    ('enabled', timedelta(minutes=60), True), ('paused', timedelta(), False),
    ('archived', timedelta(), False), ('enabled', timedelta(minutes=91), False),
    ('enabled', -timedelta(minutes=1), False), ('unknown', timedelta(), False)])
def test_pending_ad_requires_current_enabled_campaign_not_just_old_evidence(warehouse,state,age,expected):
    db, settings, _, _ = warehouse
    campaign = db.scalar(select(Campaign))
    campaign.state=state
    campaign.updated_at=(NOW-age).replace(tzinfo=None)
    db.commit()
    reason=worker._proactive_delivery_reason(db, settings, 'app:old-alert', active_payload(),NOW.replace(tzinfo=None))
    assert (reason is None) is expected


def test_queued_mixed_daily_and_ads_recheck_authorization_but_questions_can_ask_why(warehouse):
    db, settings, store, _ = warehouse
    store.status=2
    db.commit()
    payload=active_payload()
    assert worker._proactive_delivery_reason(db, settings, 'app:old-alert', payload,NOW.replace(tzinfo=None))
    assert worker._proactive_delivery_reason(db, settings, 'scheduled-daily:old', payload,NOW.replace(tzinfo=None))
    reply={**payload,'card': {'body':{}}, 'delivery': {'role':'private_question','reply_to_message_id':'om_q'}}
    assert worker._proactive_delivery_reason(db, settings, 'group-question:om_q:0',reply,NOW.replace(tzinfo=None)) is None


def test_daily_historical_ads_are_exempt_from_current_campaign_gate_but_not_store_authorization(warehouse):
    db, settings, _, _ = warehouse
    db.scalar(select(Campaign)).state='paused'
    db.commit()
    assert worker._proactive_delivery_reason(db, settings,'scheduled-daily:history',active_payload(),NOW.replace(tzinfo=None)) is None


def test_worker_skips_already_queued_quality_pages_without_opening_feishu_client(warehouse,monkeypatch):
    db, settings, _, sessions=warehouse
    monkeypatch.setattr(worker,'get_settings',lambda:settings)
    monkeypatch.setattr(worker,'SessionLocal',sessions)
    monkeypatch.setattr('jx_alert.notifications.feishu_app.FeishuAppClient.from_settings',
        lambda _:pytest.fail('Suppressed quality pages cannot call Feishu'))
    row=NotificationOutbox(idempotency_key='app:old-quality',payload={
        **active_payload(),'text':'3个活动缺少完整基线或成熟归因证据'},next_attempt_at=NOW.replace(tzinfo=None))
    db.add(row)
    db.commit()
    assert worker.process_outbox_batch()['sent']==0
    db.expire_all()
    assert db.get(NotificationOutbox,row.id).status=='SKIPPED'
    assert db.scalar(select(NotificationLog.status).where(NotificationLog.dedupe_key=='app:old-quality'))=='SKIPPED'


def test_eight_snapshot_is_frozen_until_nine_and_cannot_send_early(warehouse, monkeypatch):
    db, settings, store, sessions = warehouse
    eight = NOW - timedelta(hours=1)
    db.scalar(select(StorePerformance)).captured_at = eight.replace(tzinfo=None)
    db.commit()
    args = dict(store_dates={store.sid: DAY}, delivery_date=NOW.date(), focused_morning=True)
    assert enqueue_daily_snapshots(db, settings, **args, now=eight)['reason'] == 'before_scheduled_time'
    prepared = enqueue_daily_snapshots(db, settings, **args, now=eight, prepare=True)
    db.commit()
    row = db.get(NotificationOutbox, prepared['outbox_ids'][0])
    assert row.next_attempt_at == NOW.replace(tzinfo=None)
    assert row.payload['scheduled_not_before'] == '2026-11-18T09:00:00+08:00'
    assert worker._scheduled_window(row.idempotency_key, row.payload, eight.replace(tzinfo=None))[0] == 'WAITING'
    assert worker._scheduled_window(row.idempotency_key, row.payload, NOW.replace(tzinfo=None))[0] == 'READY'

    db.scalar(select(StorePerformance)).ahr_score = 999
    db.commit()
    assert enqueue_daily_snapshots(db, settings, **args, now=NOW)['outbox_ids'] == prepared['outbox_ids']
    db.expire_all()
    assert '评分 256' in json.dumps(db.get(NotificationOutbox, row.id).payload, ensure_ascii=False)
    assert '999' not in json.dumps(db.get(NotificationOutbox, row.id).payload, ensure_ascii=False)

    instant = eight
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(worker, 'datetime', Clock)
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    sent = []
    monkeypatch.setattr('jx_alert.notifications.feishu_app.FeishuAppClient.from_settings',
        lambda _: SimpleNamespace(send_card=lambda value, **kwargs: sent.append(value) or {'message_id': 'om_daily'}))
    assert worker.process_outbox_batch()['sent'] == 0 and sent == []
    instant = NOW
    assert worker.process_outbox_batch()['sent'] == 1 and len(sent) == 1
