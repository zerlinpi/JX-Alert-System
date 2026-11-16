"""Read-only natural question routing; never invent a shop or grant collection rights."""
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import re
import unicodedata

from sqlalchemy import select

from jx_alert.notifications.cards import card
from jx_alert.notifications.morning_digest import _health_chunks, morning_cards, select_morning_dates
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
    ads = bool(re.search(r'广告|推广|投放|ctr|cpc|cdr|cvr|cps|roas|cpa|tcoas|tacos|(?<![a-z0-9])(?:sp|sb|sd|ppc)(?![a-z0-9])', q))
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
        rows.append((store.name, actual or '暂无可用记录', labels[0], labels[1]))
    title = '店铺历史数据清单' if historical else '当前运营店铺数据'
    if not rows:
        return [card(title, subtitle='当前范围没有开启采集或通知的店铺。', sections=[])]
    cards = []
    for offset in range(0, len(rows), 10):
        value = card(title, subtitle=f'共{len(rows)}家 · 第{offset // 10 + 1}页', sections=[
            ('已保存数据', ['店铺', '最近可用报表日', '销量', '商品推广'], rows[offset:offset + 10])])
        if not card_fits(value):
            raise ValueError('Store inventory page exceeds the card limit')
        cards.append(value)
    return cards


def global_question_cards(db, settings, question, snapshots, matched_ids, *, now=None):
    from jx_alert.bot_portfolio import campaign_cards, is_ranking, metric_cards, period_length, portfolio_query
    from jx_alert.bot_service import _country_matches, _name_parts, _requested_countries

    q = unicodedata.normalize('NFKC', question).strip()
    whole_scope = bool(re.search(r'所有.{0,4}店铺|全部.{0,4}店铺|各个?店铺|每个店铺|全店|所有站点|全部站点', q))
    broad = whole_scope or bool(re.search(r'哪些店铺|哪[几一]?家店|哪个店铺|店铺列表|店铺清单|几家店|多少家店', q))
    inventory = bool(re.search(r'有哪些店铺|哪些店铺.{0,12}(?:数据|记录)|店铺列表|店铺清单|几家店|多少家店|目前.{0,8}店铺.{0,8}数据|现在.{0,8}店铺.{0,8}数据|(?:现在|目前).{0,4}有哪些数据', q))
    market = _requested_countries(q.casefold())
    topic = question_topic(q)
    query = portfolio_query(q)
    period = period_length(q)
    activity_query = bool(query and query[0] == 'campaigns')
    single_portfolio = len(matched_ids) == 1 and bool(activity_query or (query and period != 1)) and not whole_scope
    if matched_ids and not whole_scope and not single_portfolio:
        return None
    # Failed concrete ID or brand matches must never silently become an all-store query.
    name_query = re.sub(r'\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?|\d{1,2}月\d{1,2}日|(?:top|前)\s*\d+', '', q, flags=re.I)
    if not matched_ids and any(part.isascii() and len(part) >= 3 for part in _name_parts(name_query)):
        return None
    conceptual = not (broad or inventory or market or is_ranking(q)) and bool(re.search(r'什么意思|是什么|如何计算|怎么算|怎么计算|如何理解|意味着什么', q))
    subject = bool(re.search(r'店铺|站点|订单|销量|销售|广告|推广|投放|绩效|健康|经营|日报|数据|异常', q))
    requested_review = topic == 'health' or (topic == 'ads' and bool(re.search(r'异常|风险|正常', q)))
    if conceptual or not (broad or inventory or query or requested_review or (market and subject)):
        return None
    if single_portfolio:
        chosen = set(matched_ids)
    else:
        candidates = snapshots if market else operating_snapshots(settings, snapshots, q)
        chosen = {s['store_id'] for s in candidates if _country_matches(q.casefold(), s.get('country') or '待确认')}
    stores = list(db.scalars(select(Store).where(Store.sid.in_(chosen)).order_by(Store.name, Store.sid)))
    historical = include_paused(q) or bool(market and chosen - {
        r['store_id'] for r in operating_snapshots(settings, snapshots, q)
    })
    if inventory and query is None and not re.search(r'异常|风险|违规|健康|绩效|广告.*(?:问题|分析|情况)', q):
        return inventory_cards(db, settings, stores, historical=historical)
    if not stores:
        return [card('查询店铺数据', subtitle='所问范围暂无可读取的店铺记录。', sections=[])]
    if activity_query:
        return campaign_cards(db, stores, q, detail=single_portfolio)
    clock = now or datetime.now(timezone.utc)
    send_day = clock.astimezone(ZoneInfo('Asia/Shanghai')).date()
    if topic == 'health':
        return [value for value, _ in _health_chunks(db, settings, stores, send_day, clock)]
    if not period or period > 14 or (period > 1 and query is None):
        return [card('请指定报表日', subtitle='请选择具体报表日，或查询最近2至14天的花费、销量、订单等数值。', sections=[])]
    plan = store_report_plan([{'sid': s.sid, 'status': s.status} for s in stores], settings.store_timezones_file,
        now=clock, include_inactive=True,
        allow_unknown_timezone_history=getattr(settings, 'allow_unconfigured_timezone_history', False))
    from jx_alert.bot_service import _question_date, QuestionDateError
    targets, history_allowed, warnings = {}, set(), {}
    for item in plan:
        if item['state'] != 'READY':
            continue
        completed = datetime.fromisoformat(item['report_date']).date()
        try:
            explicit = _question_date(q, completed, timezone_confirmed=item['timezone_confirmed'])
        except QuestionDateError as exc:
            return [card('请确认报表日期', subtitle=str(exc), sections=[])]
        sid = item['store_id']
        targets[sid] = explicit or completed
        if targets[sid] > completed:
            warnings[sid] = '查询报表日期尚未结束，不能作为完整日报分析'
        elif explicit is None:
            history_allowed.add(sid)
    if query:
        # Compare the exact target day. A historical fallback from different dates
        # cannot replace it in a portfolio comparison or ranking.
        return metric_cards(db, settings, stores, q, query, targets, warnings, period=period)
    dates, warnings = select_morning_dates(db, [s for s in stores if s.sid in targets],
        targets, warnings, history_allowed)
    ready = [s for s in stores if s.sid in dates]
    topic = question_topic(q)
    cards = [value for value, _ in morning_cards(db, settings, ready, dates,
        send_day=send_day, now=clock,
        target_dates=targets, warnings=warnings)] if ready else []
    if topic == 'ads':
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
    from jx_alert.bot_presentation import reply_mode
    context['operator_compact'] = True
    context['reply_mode'] = reply_mode(question)
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
