from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.bot_alert_history import recent_alert_cards
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import Campaign, Store


NOW = datetime(2026, 11, 19, 6, tzinfo=timezone.utc)
QUESTION = '最近发过哪些广告告警'
CAMPAIGN = 'T+人群属性词根打包+br+0.85'


def _warehouse():
    engine = create_engine('sqlite://')
    for model in (Campaign, NotificationOutbox):
        model.__table__.create(engine)
    return engine


def _receipt(key, payload, *, status='DELIVERED', kind='chat_id'):
    return NotificationOutbox(idempotency_key=key, status=status,
        created_at=(NOW - timedelta(days=1)).replace(tzinfo=None),
        delivered_at=datetime(2026, 11, 18, 13, 3, 11),
        payload={**payload, 'hourly_ad_monitor': True, 'delivery': {'receive_id_type': kind}})


def _item(cid):
    return {'store_id': '5174', 'ad_type': 'sp', 'campaign_id': cid, 'store': 'AirBird-主账号-US',
            'campaign': cid, 'signal': '点击率下降', 'numbers': 'CTR 0.68%，参考 1.22%'}


def test_legacy_sent_ad_alert_recovers_the_native_table_from_its_receipt():
    payload = {'store_id': '5174', 'ad_campaign_id': 'airbird-ctr', 'ad_campaign_type': 'sp',
        'card': {'header': {'title': {'content': '点击率下降'}}, 'body': {'elements': [
            {'tag': 'markdown', 'content': f'**AirBird-主账号-US**\n{CAMPAIGN}\n'
                '日报花费 美元 30.87 · 点击 28\nCTR 0.68%，参考 1.22%\n请负责人复核该活动。'}]}}}
    store = Store(sid='5174', name='AirBird-主账号-US')
    with Session(_warehouse()) as db:
        db.add(Campaign(store_id=store.sid, campaign_type='sp', campaign_id='airbird-ctr', name=CAMPAIGN))
        db.add_all([_receipt('legacy', payload),
            _receipt('legacy-outside-scope', {**payload, 'store_id': '9999'})])
        db.commit()
        result, = recent_alert_cards(db, [store], QUESTION, NOW)
        item, = result['body']['elements']
        row, = item['rows']
        assert item['tag'] == 'table'
        assert row == {'c0': f'AirBird-主账号-US\n{CAMPAIGN}', 'c1': '看到广告的人更少点击',
            'c2': '发送时间 2026-11-18 21:03（北京时间）\n日报花费 美元 30.87 · 点击 28\nCTR 0.68%，参考 1.22%'}


def test_history_uses_the_confirmed_group_rows_when_a_receipt_has_a_manifest():
    store = Store(sid='5174', name='AirBird-主账号-US')
    with Session(_warehouse()) as db:
        db.add_all([
            _receipt('filtered', {'hourly_items': [_item('排队后被移除'), _item('群内已发送')],
                'hourly_delivered_items': [_item('群内已发送')]}),
            _receipt('empty', {'hourly_items': [_item('未发送活动')], 'hourly_delivered_items': []}),
            _receipt('only-subscriber', {'hourly_items': [_item('只发给订阅者')]}, kind='open_id'),
            _receipt('failed', {'hourly_delivered_items': [_item('发送失败')]}, status='DEAD'),
            _receipt('expired', {'hourly_items': [_item('已过期')]}, status='EXPIRED'),
            _receipt('earlier-batch-format', {'hourly_items': [_item('旧批次已发送')]}),
        ])
        db.commit()
        output = json.dumps(recent_alert_cards(db, [store], QUESTION, NOW), ensure_ascii=False)
        assert all(value in output for value in ('群内已发送', '旧批次已发送'))
        assert all(value not in output for value in (
            '排队后被移除', '未发送活动', '只发给订阅者', '发送失败', '已过期'))
