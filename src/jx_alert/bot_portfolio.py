"""Read scoped portfolio statistics directly from finalized warehouse observations."""
from collections import Counter, defaultdict
from datetime import timedelta, timezone
import math
import re

from sqlalchemy import func, select

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.notifications.cards import ad_kpi_value, card, number
from jx_alert.storage.collection_attempts import collection_issue_for_date
from jx_alert.storage.coverage import SALES_DATA_LABELS, store_sales_data_status
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, RawSnapshot


AD_TYPES = {'sp': '商品推广', 'sb': '品牌推广', 'sd': '展示推广'}
_KPI_LABELS = {'CTR': '点击率', 'CPC': '单次点击花费', 'CDR': '广告转化率', 'CPS': '广告归因每件成本',
               'CPA': '广告每单成本', 'ROAS': '广告投入产出比', 'TCOAS': '全店广告花费占比'}
_RANK = r'排名|排行|最高|最多|最大|最好|最低|最少|最小|前\s*\d+|top\s*\d+|哪[家个]|对比|比较'
_DATED = r'今日|今天|当日|昨日|昨天|前天|\d{1,2}月\d{1,2}日|\d{4}[-/]\d'


def period_length(question):
    match = re.search(r'(?:最近|过去|近)\s*(\d+|[一二三四五六七八九十两]+)\s*(?:天|日)', question)
    if match:
        token = match[1]
        if token.isdecimal():
            return int(token) if len(token) <= 2 else 0
        digits = dict(zip('一二三四五六七八九', range(1, 10), strict=True)) | {'两': 2}
        if '十' in token:
            left, _, right = token.partition('十')
            return digits.get(left, 0 if left else 1) * 10 + digits.get(right, 0) if len(left) <= 1 and len(right) <= 1 else 0
        return digits.get(token, 0)
    if re.search(r'本周|上周|本月|上月|(?:最近|过去|近)\s*(?:\d+|[一二三四五六七八九十两]+)\s*(?:周|月)', question):
        return 0
    return 1


def ad_types(question):
    return tuple(code for code, pattern in (
        ('sp', r'商品推广|(?<![a-z0-9])sp(?![a-z0-9])'),
        ('sb', r'品牌推广|(?<![a-z0-9])sb(?![a-z0-9])'),
        ('sd', r'展示推广|(?<![a-z0-9])sd(?![a-z0-9])'),
    ) if re.search(pattern, question, re.I))


def portfolio_query(question):
    """Recognize reportable facts independently of a required shop-name template."""
    selected = ad_types(question)
    if re.search(r'异常|违规|风险|是否正常|怎么样|好不好|采集.*进度|同步.*进度', question):
        return None
    ads = bool(selected or re.search(r'广告|推广|投放|ppc', question, re.I))
    if (ads and re.search(r'在投|有投放|投了|启用|投放店铺', question)
            and not re.search(_RANK + r'|花费|支出|曝光|点击|订单|销售额|销量', question, re.I)):
        return ('ad_impressions', '广告曝光') if re.search(_DATED, question) or period_length(question) != 1 else ('campaigns', '启用广告活动')
    for code, label in _KPI_LABELS.items():
        if re.search(r'(?<![a-z0-9])' + code + r'(?![a-z0-9])|' + label, question, re.I):
            return code, label
    if ads:
        for pattern, metric, title in (
            (r'花费|支出|消费|消耗|费用|花[了多少]|spend', 'spend', '广告花费'),
            (r'曝光|展示量|impressions', 'impressions', '广告曝光'),
            (r'点击|clicks', 'clicks', '广告点击'),
            (r'销售额|成交额|营收|收入|销售', 'sales', '广告归因销售额'),
            (r'销量|售出', 'units', '广告归因销量'),
            (r'出单|订单|转化', 'orders', '广告归因订单'),
        ):
            if re.search(pattern, question, re.I):
                return 'ad_' + metric, title
        if not re.search(r'绩效|健康', question):
            return 'ad_spend', '广告花费'
    for pattern, metric, title in (
        (r'销售额|成交额|营收|销售|卖得|营业额|收入', 'sales', '店铺销售额'),
        (r'销量|售出|卖了多少件', 'units', '店铺销量'),
        (r'出单|订单|多少单|下单', 'orders', '店铺订单'),
    ):
        if re.search(pattern, question):
            return metric, title
    return None


def is_ranking(question):
    return bool(re.search(_RANK, question, re.I))


def campaign_cards(db, stores, question, *, detail=False):
    """Use the last collected campaign configuration and label that observation date."""
    kinds = ad_types(question) or tuple(AD_TYPES)
    scope = {s.sid: s for s in stores if s.status == 1}
    rows = db.scalars(select(Campaign).where(Campaign.store_id.in_(scope), Campaign.campaign_type.in_(kinds))
        .order_by(Campaign.name, Campaign.campaign_id))
    enabled = [r for r in rows if (r.state or '').casefold() == 'enabled']
    configured = Counter((r.store_id, r.campaign_type) for r in enabled)
    sources = {'lingxing_' + k + '_campaigns': k for k in kinds}
    observations = {}
    for sid, source, stamp in db.execute(select(RawSnapshot.store_id, RawSnapshot.source, func.max(RawSnapshot.captured_at))
            .where(RawSnapshot.store_id.in_(scope), RawSnapshot.source.in_(sources))
            .group_by(RawSnapshot.store_id, RawSnapshot.source)):
        observations[sid, sources[source]] = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    active, missing = [], 0
    for store in stores:
        available = {kind: observations.get((store.sid, kind)) for kind in kinds}
        observed = []
        for kind, stamp in available.items():
            count = configured[store.sid, kind]
            if count:
                when = stamp.strftime('%m-%d %H:%M') + ' UTC' if stamp else '采集时间待确认'
                observed.append(f'{AD_TYPES[kind]} {count}个（{when}）')
        if observed:
            active.append((store.name, '；'.join(observed)))
        if not all(available.values()):
            missing += 1
    notes = ['启用状态取自最近保存的广告活动配置，实际曝光请按报表日查询。']
    if missing:
        notes.append(f'{missing}家缺少至少一种相关广告配置观测。')
    if detail and enabled:
        rows = [(r.name or r.campaign_id, AD_TYPES[r.campaign_type], r.updated_at.strftime('%m-%d %H:%M') + ' UTC')
                for r in enabled[:30]]
        if len(enabled) > len(rows):
            notes.append('此处列出前30个活动。')
        return [card('已观测到启用的广告活动', subtitle=f'{stores[0].name} · 共{len(enabled)}个',
            sections=[('活动', ['广告活动', '类型', '状态观测时间'], rows[offset:offset + 10])],
            notes=notes if offset == 0 else []) for offset in range(0, len(rows), 10)]
    title = '已观测到启用广告的店铺'
    if not active:
        return [card(title, subtitle='现有配置观测未列出启用活动。', sections=[], notes=notes)]
    return [card(title, subtitle=f'共{len(active)}家',
        sections=[('按店铺', ['店铺', '启用活动与观测时间'], active[offset:offset + 10])],
        notes=notes if offset == 0 else []) for offset in range(0, len(active), 10)]


def _shop_metric(db, store, on, metric, issue):
    facts = db.scalars(select(DailyMetric).where(DailyMetric.store_id == store.sid, DailyMetric.metric_date == on,
        DailyMetric.dimension == 'store', DailyMetric.entity_id == store.sid)).all()
    records = {r.dimension: r for r in db.scalars(select(DataCoverage).where(DataCoverage.store_id == store.sid,
        DataCoverage.metric_date == on, DataCoverage.dimension.in_(['store', 'sku_asin', 'sku_msku'])))}
    state = store_sales_data_status(store.status, records.get('store'), facts,
        records.get('sku_asin'), records.get('sku_msku'), issue=issue)
    if state not in {'AVAILABLE', 'ZERO'}:
        return None, None, SALES_DATA_LABELS[state]
    row = next(r for r in facts if r.metric_name == metric)
    if metric == 'sales' and not re.fullmatch(r'[A-Z]{3}', row.currency or ''):
        return None, None, '销售币种待确认'
    return row.value, row.currency if metric == 'sales' else None, None


def _advertising_metric(db, store, on, metric, kinds):
    from jx_alert.bot_service import _ad_totals
    rows = [r for r in _ad_totals(db, store.sid, on) if r['ad_type'] in {AD_TYPES[k] for k in kinds}]
    if any(r['coverage'] in {'FAILED', 'PARTIAL_FAILED', 'INTERRUPTED'} for r in rows):
        return None, None, '广告采集失败'
    if any(r['coverage'] == 'MISSING' for r in rows):
        return None, None, '广告报表尚未采集'
    records = [r for r in rows if not r['no_records']]
    codes = {r[metric + '_currency'] for r in records} if metric in {'spend', 'sales'} else {None}
    if not records:
        return None, None, '广告报表无记录'
    if len(codes) > 1:
        return None, None, '广告币种不一致'
    if any(r[metric] is None for r in records):
        return None, None, '广告指标或币种不完整'
    return math.fsum(r[metric] for r in records), next(iter(codes)), None


def _period_metric(db, store, end, metric, kinds, period):
    start = end - timedelta(days=period - 1)
    values, currencies = [], set()
    for offset in range(period):
        on = start + timedelta(days=offset)
        issue = collection_issue_for_date(db, store.sid, on)
        if issue == 'DAILY_FAILED':
            return None, None, '采集失败', '', []
        if metric.startswith('ad_'):
            amount, currency, gap = _advertising_metric(db, store, on, metric[3:], kinds or tuple(AD_TYPES))
        else:
            amount, currency, gap = _shop_metric(db, store, on, metric, issue)
        if gap:
            return None, None, gap, '', []
        values.append((on, amount))
        currencies.add(currency)
    if len(currencies) != 1:
        return None, None, '期间币种不一致', '', []
    day = start.isoformat() if start == end else f'{start} 至 {end}'
    return math.fsum(value for _, value in values), next(iter(currencies)), None, day, values


def metric_cards(db, settings, stores, question, query, targets, warnings, *, period=1):
    metric, title = query
    kinds = ad_types(question)
    if metric.startswith('ad_') and kinds:
        title = '/'.join(AD_TYPES[k] for k in kinds) + title.removeprefix('广告')
    elif metric in _KPI_LABELS and kinds:
        return [card(title, subtitle='按广告类型查询该指标时，请同时提供店铺和广告活动名称。', sections=[])]
    if period > 1 and metric in _KPI_LABELS:
        return [card(title, subtitle='该比率按报表日及成熟归因窗口计算，请指定结束日期。', sections=[])]
    buckets = defaultdict(list)
    gaps = Counter()
    positive_only = metric == 'ad_impressions' and bool(re.search(r'在投|有投放|投了|启用|投放店铺', question))
    trend = period > 1 and bool(re.search(r'趋势|每天|每日|分日|逐日', question))
    for store in stores:
        on = targets.get(store.sid)
        if store.status != 1:
            gaps['授权失败' if store.status == 2 else '店铺未启用'] += 1
            continue
        if on is None or store.sid in warnings:
            gaps[warnings.get(store.sid) or '报表日期待确认'] += 1
            continue
        if metric in _KPI_LABELS:
            if collection_issue_for_date(db, store.sid, on) == 'DAILY_FAILED':
                gaps['采集失败'] += 1
                continue
            item = advertising_kpis(db, settings, store.sid, on)[metric]
            if item['current'] is None:
                gaps['指标证据不足'] += 1
                continue
            amount, currency, day, display = item['current'], item['currency'], item['current_date'], ad_kpi_value(item)
        else:
            amount, currency, gap, day, values = _period_metric(db, store, on, metric, kinds, period)
            if gap:
                gaps[gap] += 1
                continue
            display = number(amount, money=currency is not None, currency=currency or '')
            if trend:
                display += '\n' + '；'.join(f'{d:%m-%d} {number(v, money=bool(currency))}' for d, v in values)
        if not positive_only or amount > 0:
            buckets[day, currency].append((store.name, amount, display))
    ranked = is_ranking(question)
    lowest = bool(re.search(r'最低|最少|最小', question))
    limit = re.search(r'(?:前|top)\s*(\d+)', question, re.I)
    count = max(1, min(int(limit[1]), 50)) if limit else (
        1 if re.search(r'哪[家个].*(?:最高|最多|最大|最好|最低|最少|最小)', question) else 10 if ranked else None)
    notes = ['未参与比较：' + '、'.join(f'{label} {n}家' for label, n in gaps.items())] if gaps else []
    if period > 1:
        notes.append('期间合计仅包含每天均有完整证据的店铺。')
    if metric in {'ad_orders', 'ad_units', 'ad_sales'}:
        notes.append('归因报表指标会随归因窗口更新。')
    cards = []
    for (day, currency), records in sorted(buckets.items(), key=lambda pair: (pair[0][0], pair[0][1] or '')):
        records.sort(key=(lambda row: (row[1] if lowest else -row[1], row[0])) if ranked else lambda row: row[0])
        total = f' · 已验证合计 {number(math.fsum(row[1] for row in records), money=bool(currency), currency=currency or "")}' if (
            metric not in _KPI_LABELS and re.search(r'总计|总共|一共|合计|总花费|总销售额|总订单', question)) else ''
        rows = [(name, display) for name, _, display in records[:count]]
        for offset in range(0, len(rows), 10):
            cards.append(card(title + ('排名' if ranked else ''),
                subtitle=f'{day} · 已验证{len(records)}/{len(stores)}家' + (f' · {currency}' if currency else '') + total,
                sections=[('按店铺', ['店铺', title], rows[offset:offset + 10])], notes=notes if not cards else []))
    empty = '查询范围暂无可核实的投放曝光。' if positive_only else '查询范围暂无可核实的数值。'
    return cards or [card(title, subtitle=empty, sections=[], notes=notes)]
