"""Show recent warehouse alert records inside the originally authorized chat."""
from datetime import datetime, timedelta, timezone
import re

from sqlalchemy import select

from jx_alert.bot_portfolio import ad_types, period_length
from jx_alert.notifications.cards import card
from jx_alert.notifications.hourly_ads import BEIJING, TITLES, advertising_card, campaign_identity
from jx_alert.notifications.outbox import NotificationOutbox


def recent_alert_lookup(question):
    return (bool(re.search(r'最近|近期|过去|近\s*(?:\d+|[一二三四五六七八九十两]+)\s*[天日]', question))
            and bool(re.search(r'广告.{0,8}异常|异常.{0,8}广告|广告告警', question))
            and bool(re.search(r'有没有|有无|是否有|哪些|列举|列出|汇总', question))
            and not re.search(r'为什么|为何|如何|怎么办|预测|诊断|重新分析', question))


def recorded_alert_lookup(question):
    return recent_alert_lookup(question) and bool(
        re.search(r'已发送|发过|已提醒|提醒过|已记录|告警记录|通知记录', question))


def recent_alert_cards(db, stores, question, now):
    explicit_period = re.search(r'(?:最近|过去|近)\s*(?:\d+|[一二三四五六七八九十两]+)\s*[天日]', question)
    parsed = period_length(question)
    days = parsed if explicit_period or parsed == 0 else 7
    if not 1 <= days <= 14:
        return [card('请确认查询范围', subtitle='可以查询最近 1 到 14 天的广告异常记录。', sections=[])]
    scope = {store.sid for store in stores}
    kinds = set(ad_types(question)) or {'sp', 'sb', 'sd'}
    threshold = (now - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
    batches = db.scalars(select(NotificationOutbox.payload).where(
        NotificationOutbox.status == 'DELIVERED', NotificationOutbox.created_at >= threshold,
        NotificationOutbox.payload['hourly_ad_monitor'].as_boolean() == True,  # noqa: E712
        NotificationOutbox.payload['delivery']['receive_id_type'].as_string() == 'chat_id',
    ).order_by(NotificationOutbox.id.desc()))
    items = {}
    for batch in batches:
        for item in batch.get('hourly_items', []):
            if item.get('store_id') not in scope or item.get('ad_type') not in kinds:
                continue
            identity = campaign_identity(item)
            if identity in items:
                continue
            value = dict(item)
            if value['signal'] in {'盘中花费超出复核线', '盘中花费超预算', '盘中花费需要关注', TITLES['intraday_budget']}:
                value['signal'] = '当时的花费远超平均日预算'
                value['numbers'] = re.sub(r'^花费 |^今天已花 ', '当天已花 ', value['numbers'], flags=re.M)
                value['numbers'] = re.sub(r'^日预算 ', '平均日预算 ', value['numbers'], flags=re.M)
            stamp = value.get('detected_at')
            if stamp:
                try:
                    on = datetime.fromisoformat(stamp).astimezone(BEIJING).date()
                except ValueError:
                    pass
                else:
                    value['numbers'] = f'发现日期 {on}\n' + value['numbers']
            items[identity] = value
    title = f'最近 {days} 天已记录的广告异常'
    if not items:
        return [card(title, subtitle='这个范围内没有找到已记录的异常。', sections=[])]
    rows = list(items.values())
    return [advertising_card(rows[offset:offset + 10], title=title) for offset in range(0, len(rows), 10)]
