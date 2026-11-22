"""Show recent warehouse alert records inside the originally authorized chat."""
from datetime import datetime, timedelta, timezone
import re

from sqlalchemy import func, select

from jx_alert.bot_portfolio import ad_types, period_length
from jx_alert.notifications.cards import card
from jx_alert.notifications.hourly_ads import BEIJING, TITLES, advertising_card, campaign_identity
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import Campaign


_LEGACY_SIGNALS = {
    '点击率下降': TITLES['CTR'], '点击花费上升': TITLES['CPC'], '点击转化率下降': TITLES['CDR'],
    '单件转化花费上升': TITLES['CPS'], '单均花费上升': TITLES['CPA'], '广告投入产出下降': TITLES['ROAS'],
    '广告花费上升': TITLES['spend'], '成熟归因订单下降': TITLES['orders'],
    '成熟广告有花费无转化': TITLES['zero_conversion'], '盘中花费需要关注': TITLES['intraday_budget'],
}
_AMOUNT = r'[\d,]+(?:\.\d+)?'
_MONEY = rf'(?:[A-Z]{{3}}|美元|人民币|欧元|英镑|日元|加拿大元|澳元|墨西哥比索|巴西雷亚尔) {_AMOUNT}'
_LEGACY_METRIC = re.compile(
    rf'(?:日报花费|成熟归因花费|盘中花费) {_MONEY}(?: · (?:点击 {_AMOUNT}|日预算 {_MONEY}))?'
    rf'|归因订单 {_AMOUNT}|(?:CTR|CDR) {_AMOUNT}%，参考 {_AMOUNT}%'
)


def recent_alert_lookup(question):
    return (bool(re.search(r'最近|近期|过去|近\s*(?:\d+|[一二三四五六七八九十两]+)\s*[天日]', question))
            and bool(re.search(r'广告.{0,8}异常|异常.{0,8}广告|广告告警', question))
            and bool(re.search(r'有没有|有无|是否有|哪些|列举|列出|汇总', question))
            and not re.search(r'为什么|为何|如何|怎么办|预测|诊断|重新分析', question))


def recorded_alert_lookup(question):
    return recent_alert_lookup(question) and bool(
        re.search(r'已发送|发过|已提醒|提醒过|已记录|告警记录|通知记录', question))


def _legacy_item(db, payload, stores, kinds):
    """Read a historical single-activity card without consulting current metrics."""
    sid, kind, cid = (payload.get(key) for key in ('store_id', 'ad_campaign_type', 'ad_campaign_id'))
    if sid not in stores or kind not in kinds or not isinstance(cid, str) or not cid:
        return None
    name = db.scalar(select(Campaign.name).where(
        Campaign.store_id == sid, Campaign.campaign_type == kind, Campaign.campaign_id == cid))
    original = payload.get('card') or {}
    old_title = original.get('header', {}).get('title', {}).get('content') or ''
    signals = [_LEGACY_SIGNALS[part] for part in old_title.split('；') if part in _LEGACY_SIGNALS]
    # The archived card mixed measurements and model prose. Copy only the
    # operands from known historical line formats and retain their old values.
    metrics = [line for el in original.get('body', {}).get('elements', [])
               if el.get('tag') == 'markdown' for line in el.get('content', '').splitlines()
               if _LEGACY_METRIC.fullmatch(line)]
    return {'store_id': sid, 'ad_type': kind, 'campaign_id': cid,
            'store': stores[sid], 'campaign': name or cid,
            'signal': '；'.join(signals) or '广告异常', 'numbers': '\n'.join(dict.fromkeys(metrics))}


def recent_alert_cards(db, stores, question, now, *, report_delivery):
    """Read only receipts from the currently authorized report-group generation."""
    explicit_period = re.search(r'(?:最近|过去|近)\s*(?:\d+|[一二三四五六七八九十两]+)\s*[天日]', question)
    parsed = period_length(question)
    days = parsed if explicit_period or parsed == 0 else 7
    if not 1 <= days <= 14:
        return [card('请确认查询范围', subtitle='可以查询最近 1 到 14 天的广告异常记录。', sections=[])]
    scope = {store.sid: store.name for store in stores}
    kinds = set(ad_types(question)) or {'sp', 'sb', 'sd'}
    clock = now.astimezone(timezone.utc).replace(tzinfo=None)
    threshold = clock - timedelta(days=days)
    current_chat = report_delivery.get('receive_id') if isinstance(report_delivery, dict) else None
    if (not isinstance(current_chat, str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]+', current_chat)
            or report_delivery.get('receive_id_type') != 'chat_id'):
        return [card('通知群范围未确认', subtitle='未读取历史告警。', sections=[])]
    sent_at = func.coalesce(NotificationOutbox.delivered_at, NotificationOutbox.created_at)
    batches = db.execute(select(NotificationOutbox.payload, NotificationOutbox.delivered_at,
        NotificationOutbox.created_at).where(
        NotificationOutbox.status == 'DELIVERED', sent_at.between(threshold, clock),
        NotificationOutbox.payload['hourly_ad_monitor'].as_boolean() == True,  # noqa: E712
        NotificationOutbox.payload['delivery']['receive_id_type'].as_string() == 'chat_id',
        NotificationOutbox.payload['delivery']['receive_id'].as_string() == current_chat,
    ).order_by(sent_at.desc(), NotificationOutbox.id.desc()))
    items = {}
    for batch, delivered_at, created_at in batches:
        previous = batch['delivery']
        generation = report_delivery.get('routing_changed_at')
        if (previous.get('routing_changed_at') != generation
                or previous.get('routing_alias', report_delivery.get('routing_alias'))
                != report_delivery.get('routing_alias')):
            continue
        if 'hourly_delivered_items' in batch:
            rows = batch['hourly_delivered_items']
        elif 'hourly_items' in batch:
            rows = batch['hourly_items']
        else:
            rows = [_legacy_item(db, batch, scope, kinds)]
        for item in rows or ():
            if (not isinstance(item, dict) or item.get('store_id') not in scope
                    or item.get('ad_type') not in kinds or not item.get('campaign_id')):
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
            sent = (delivered_at or created_at).replace(tzinfo=timezone.utc).astimezone(BEIJING)
            time_label = '发送时间' if delivered_at else '记录时间'
            value['numbers'] = '\n'.join(filter(None, (
                f'{time_label} {sent:%Y-%m-%d %H:%M}（北京时间）', value['numbers'])))
            items[identity] = value
    title = f'最近 {days} 天已记录的广告异常'
    if not items:
        return [card(title, subtitle='这个范围内没有找到已记录的异常。', sections=[])]
    rows = list(items.values())
    return [advertising_card(rows[offset:offset + 10], title=title) for offset in range(0, len(rows), 10)]
