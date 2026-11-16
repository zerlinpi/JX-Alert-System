"""Read-only natural question routing; never invent a shop or grant collection rights."""
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import re
import unicodedata

from sqlalchemy import select

from jx_alert.notifications.cards import card
from jx_alert.notifications.morning_digest import morning_cards
from jx_alert.notifications.store_health import card_fits
from jx_alert.operations import store_report_plan
from jx_alert.storage.collection_attempts import collection_issue_for_date
from jx_alert.storage.coverage import SALES_DATA_LABELS, store_sales_data_status
from jx_alert.storage.models import DailyMetric, DataCoverage, Store
from jx_alert.store_controls import store_control_snapshots


def include_paused(question):
    return bool(re.search(r'停用|已关闭|已停用|关闭的|含关闭|历史店铺|所有站点|全部站点', question or ''))


def operating_snapshots(settings, snapshots, question):
    if include_paused(question):
        return snapshots
    controls = store_control_snapshots(settings, [s['store_id'] for s in snapshots])
    return [s for s in snapshots if controls[s['store_id']]['collection_enabled']
            or controls[s['store_id']]['notifications_enabled']]


def question_topic(question):
    q = unicodedata.normalize('NFKC', question or '').lower()
    health = bool(re.search(r'绩效|健康|违规|侵权|通知', q))
    ads = bool(re.search(r'广告|推广|投放|ctr|cpc|cdr|cvr|cps|roas|cpa|tcoas|tacos', q))
    sales = bool(re.search(r'店铺.{0,4}(?:销量|销售额|订单)|经营|总销售|总订单', q))
    if health and not ads and not sales:
        return 'health'
    if ads and not health and not sales:
        return 'ads'
    return 'overall'


def _data_state(db, store, day):
    facts = list(db.scalars(select(DailyMetric).where(DailyMetric.store_id == store.sid,
        DailyMetric.metric_date == day, DailyMetric.dimension.in_(['store', 'campaign_sp']))))
    coverage = {r.dimension: r for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store.sid, DataCoverage.metric_date == day,
        DataCoverage.dimension.in_(['store', 'sku_asin', 'sku_msku', 'campaign_sp'])))}
    issue = collection_issue_for_date(db, store.sid, day)
    sales = store_sales_data_status(store.status, coverage.get('store'),
        [r for r in facts if r.dimension == 'store' and r.entity_id == store.sid],
        coverage.get('sku_asin'), coverage.get('sku_msku'), issue=issue)
    ads, ad_rows = coverage.get('campaign_sp'), [r for r in facts if r.dimension == 'campaign_sp']
    entities = {r.entity_id for r in ad_rows}
    usable = bool(ads and ads.status == 'COMPLETE' and entities and ads.row_count <= len(entities))
    for metric in ('spend', 'clicks', 'impressions'):
        selected = [r for r in ad_rows if r.metric_name == metric]
        usable = usable and {r.entity_id for r in selected} == entities and all(
            math.isfinite(r.value) and r.value >= 0 for r in selected)
        if metric == 'spend':
            usable = usable and len({r.currency for r in selected}) == 1 and all(
                isinstance(r.currency, str) and re.fullmatch('[A-Z]{3}', r.currency) for r in selected)
    if store.status == 2:
        ad_label, usable = '授权失败', False
    elif store.status != 1:
        ad_label, usable = '店铺未启用', False
    elif issue == 'DAILY_FAILED' or ads is not None and ads.status in {'FAILED', 'PARTIAL_FAILED'}:
        ad_label, usable = '采集失败', False
    elif usable:
        ad_label = '有广告记录'
    elif ads and ads.status == 'COMPLETE' and ads.row_count == 0 and not ad_rows:
        ad_label = '接口无记录'
    else:
        ad_label = '尚未采集' if ads is None else '数据不完整'
    return sales, usable, ad_label


def inventory_cards(db, settings, stores, *, historical=False):
    rows = []
    controls = store_control_snapshots(settings, [s.sid for s in stores])
    for store in stores:
        days = list(db.scalars(select(DataCoverage.metric_date).where(DataCoverage.store_id == store.sid,
            DataCoverage.dimension.in_(['store', 'campaign_sp'])).distinct().order_by(DataCoverage.metric_date.desc()).limit(22)))
        actual, labels = None, (('授权失败', '授权失败') if store.status == 2 else
            ('店铺未启用', '店铺未启用') if store.status != 1 else ('尚未采集', '尚未采集'))
        for index, day in enumerate(days):
            sales, sp, ad_label = _data_state(db, store, day)
            if index == 0:
                labels = (SALES_DATA_LABELS[sales], ad_label)
            if sales in {'AVAILABLE', 'ZERO'} or sp:
                actual = day.isoformat()
                break
        state = controls[store.sid]
        switches = ('采集开' if state['collection_enabled'] else '采集关') + ' / ' + (
            '通知开' if state['notifications_enabled'] else '通知关')
        rows.append((store.name, store.country or '站点待确认', actual or '暂无可用记录',
                     labels[0], labels[1], switches))
    title = '店铺历史数据清单' if historical else '当前运营店铺数据'
    if not rows:
        return [card(title, subtitle='当前范围没有开启采集或通知的店铺。', sections=[])]
    cards = []
    for offset in range(0, len(rows), 10):
        value = card(title, subtitle=f'共{len(rows)}家 · 第{offset // 10 + 1}页', sections=[
            ('已保存数据', ['店铺', '站点', '最近可用报表日', '最新销量状态', '最新商品推广状态', '开关'], rows[offset:offset + 10])])
        if not card_fits(value):
            raise ValueError('Store inventory page exceeds the card limit')
        cards.append(value)
    return cards


def global_question_cards(db, settings, question, snapshots, matched_ids, *, now=None):
    q = unicodedata.normalize('NFKC', question).strip()
    broad = bool(re.search(r'所有店铺|全部店铺|各个?店铺|每个店铺|全店|哪些店铺|店铺列表|店铺清单|几家店|多少家店', q))
    inventory = bool(re.search(r'有哪些店铺|哪些店铺.{0,12}(?:数据|记录)|店铺列表|店铺清单|几家店|多少家店|目前.{0,8}店铺.{0,8}数据|现在.{0,8}店铺.{0,8}数据|(?:现在|目前).{0,4}有哪些数据', q))
    # A named-shop question stays named-shop, including its ordinary phrasing.
    if len(matched_ids) == 1 and not re.search(r'所有店铺|全部店铺|全店|各个?店铺|每个店铺', q):
        return None
    if not broad and not inventory:
        return None
    chosen = {s['store_id'] for s in operating_snapshots(settings, snapshots, q)}
    stores = list(db.scalars(select(Store).where(Store.sid.in_(chosen)).order_by(Store.name, Store.sid)))
    if inventory and not re.search(r'异常|风险|违规|健康|绩效|广告.*(?:问题|分析|情况)', q):
        return inventory_cards(db, settings, stores, historical=include_paused(q))
    if not stores:
        return inventory_cards(db, settings, stores, historical=include_paused(q))
    clock = now or datetime.now(timezone.utc)
    plan = store_report_plan([{'sid': s.sid, 'status': s.status} for s in stores], settings.store_timezones_file,
        now=clock, include_inactive=True,
        allow_unknown_timezone_history=getattr(settings, 'allow_unconfigured_timezone_history', False))
    dates = {r['store_id']: r['report_date'] for r in plan if r['state'] == 'READY'}
    ready = [s for s in stores if s.sid in dates]
    topic = question_topic(q)
    cards = morning_cards(db, settings, ready, dates,
        send_day=clock.astimezone(ZoneInfo('Asia/Shanghai')).date(), now=clock) if ready else []
    if topic == 'health':
        cards = [v for v in cards if v['header']['title']['content'].startswith('店铺绩效')]
    elif topic == 'ads':
        cards = [v for v in cards if not v['header']['title']['content'].startswith('店铺绩效')]
    if len(ready) < len(stores):
        waiting = [(s.name, '报表日期或时区配置待确认') for s in stores if s.sid not in dates]
        cards.append(card('待补齐查询条件', subtitle='未参与广告正常/异常判断', sections=[('店铺', ['店铺', '待确认项'], waiting)]))
    return cards or [card('商品推广核对结果', subtitle='可比证据中未见达到通知门槛的活动问题。', sections=[])]


def focus_question(context, data_card, question):
    """Keep all evidence for the requested topic, omit absent optional ad types."""
    context, value = deepcopy(context), deepcopy(data_card)
    topic = question_topic(question)
    context['question_topic'] = topic
    show_optional = bool(re.search(r'品牌推广|展示推广|\bSB\b|\bSD\b', question, re.I))
    absent = {r['ad_type'] for r in context.get('advertising_totals', []) if r['ad_type'] != '商品推广'
              and (r.get('no_records') or all(r.get(m) is None for m in ('spend', 'orders', 'sales', 'clicks', 'impressions')))}
    if not show_optional:
        context['advertising_totals'] = [r for r in context.get('advertising_totals', []) if r['ad_type'] not in absent]
        context['sales_and_spend'] = [r for r in context.get('sales_and_spend', []) if not any(
            r['metric'].startswith(kind) for kind in absent)]
        context['coverage'] = [r for r in context.get('coverage', []) if r['dataset'] not in absent]
        context['ad_detail_coverage'] = [r for r in context.get('ad_detail_coverage', []) if r.get('rows')]
    elements, result, heading = value['body']['elements'], [], ''
    for item in elements:
        if item.get('tag') == 'markdown' and item.get('content', '').startswith('**'):
            heading = item['content'].strip('*')
            if topic == 'health' and heading != '店铺绩效快报' or topic == 'ads' and heading == '店铺绩效快报':
                continue
        if topic == 'health' and heading != '店铺绩效快报':
            continue
        if item.get('tag') == 'table':
            if topic == 'health' and heading != '店铺绩效快报' or topic == 'ads' and heading == '店铺绩效快报':
                continue
            if not show_optional and heading in {'经营与广告', '采集状态'}:
                item['rows'] = [r for r in item['rows'] if not any(r.get('c0', '').startswith(kind) for kind in absent)]
            if topic == 'ads' and heading == '经营与广告':
                item['rows'] = [r for r in item['rows'] if '推广' in r.get('c0', '')]
        result.append(item)
    value['body']['elements'] = result
    if topic == 'health':
        for key in ('advertising_kpis', 'advertising_totals', 'ad_detail_coverage', 'campaigns', 'screening_candidates',
                    'sales_and_spend', 'coverage', 'data_warning', 'report_date_warning'):
            context.pop(key, None)
    elif topic == 'ads':
        context.pop('health', None)
        context['sales_and_spend'] = [r for r in context.get('sales_and_spend', []) if '推广' in r['metric']]
    return context, value
