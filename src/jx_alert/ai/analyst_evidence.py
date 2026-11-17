"""Small, read-only evidence packets for questions about trends and scenarios.

The model receives facts and arithmetic, never database access or callable tools.
Scenario ranges reflect observed daily pace, not statistical confidence intervals.
"""
from calendar import monthrange
from datetime import date, timedelta
import math
import re


def analyst_question(question):
    return bool(re.search(r'预估|预测|预判|月底|月末|预算|原因|为什么|为何|拆解|下钻|浪费|补货|库存|趋势.*分析|分析.*趋势', question or ''))


def analysis_evidence(db, store, end, question, *, completed):
    """Query exact completed days; gaps stay gaps and cannot enter projections."""
    from jx_alert.bot_portfolio import _period_metric, ad_types, period_length

    end = min(end, completed)
    if store.status != 1:
        return {'through_report_date': end.isoformat(), 'daily_spend': [],
                'limitations': ['店铺授权或启用状态异常，不能用历史观测推测当前经营情况。']}
    kinds = ad_types(question) or ('sp',)
    forecast = bool(re.search(r'预估|预测|预判|月底|月末|预算', question or '') and
                    re.search(r'广告|推广|花费|支出|预算|sp\b|sb\b|sd\b', question or '', re.I))
    # Month-to-date is needed only for a monthly spending scenario. Ordinary
    # analysis needs at most fourteen observations, not entire raw API payloads.
    period = period_length(question)
    days = period if 2 <= period <= 14 else 14
    first = end.replace(day=1) if forecast else end - timedelta(days=days - 1)
    points, gaps = [], []
    for offset in range((end - first).days + 1):
        on = first + timedelta(days=offset)
        amount, currency, gap, _, _ = _period_metric(db, store, on, 'ad_spend', kinds, 1)
        if gap:
            gaps.append({'date': on.isoformat(), 'reason': gap})
        points.append({'date': on.isoformat(), 'spend': amount, 'currency': currency})
    packet = {'scope': '/'.join(kinds).upper(), 'through_report_date': end.isoformat(),
              'daily_spend': points[-14:], 'missing_days': gaps,
              'limitations': ['花费是已保存的日报观测；未包含盘中或未采集数据。']}
    if re.search(r'经营|销量|销售|收入|订单', question or ''):
        values = []
        for point in points[-14:]:
            on = date.fromisoformat(point['date'])
            metrics = {}
            for metric in ('sales', 'units', 'orders'):
                amount, code, gap, _, _ = _period_metric(db, store, on, metric, (), 1)
                metrics[metric] = {'value': amount, 'currency': code, 'gap': gap}
            values.append({'date': point['date'], **metrics})
        packet['daily_business'] = values
    if forecast:
        recent = points[-7:]
        codes = {p['currency'] for p in points if p['spend'] is not None}
        enough = len(recent) == 7 and all(p['spend'] is not None for p in recent)
        same_currency = len(codes) == 1 and bool(next(iter(codes), None))
        last = end.replace(day=monthrange(end.year, end.month)[1])
        projection = {'method': '最近7个完整报表日的日均花费保持不变',
            'scenario_only': True, 'month_end': last.isoformat(),
            'remaining_days': (last - end).days, 'currency': next(iter(codes)) if same_currency else None,
            'budget_limit': None, 'month_to_date': None, 'monthly_total': None,
            'remaining_spend': None, 'observed_pace_range': None}
        if enough and same_currency:
            pace = math.fsum(p['spend'] / 7 for p in recent)
            remaining = projection['remaining_days']
            range_values = [min(p['spend'] for p in recent) * remaining,
                            max(p['spend'] for p in recent) * remaining]
            if all(math.isfinite(value) for value in [pace, pace * remaining, *range_values]):
                projection.update(daily_pace=pace, basis_start=recent[0]['date'], basis_end=recent[-1]['date'],
                    remaining_spend=pace * remaining, observed_pace_range=range_values)
            else:
                projection['gap'] = '花费超出可计算范围，不能进行数值预估。'
            if not gaps and projection['remaining_spend'] is not None:
                try:
                    actual = math.fsum(p['spend'] for p in points)
                    if math.isfinite(actual + pace * remaining):
                        projection.update(month_to_date=actual, monthly_total=actual + pace * remaining)
                    else:
                        projection['gap'] = '月度花费超出可计算范围。'
                except OverflowError:
                    projection['gap'] = '月度花费超出可计算范围。'
            else:
                projection.setdefault('gap', '本月存在缺采日期，不能合成全月总额；仅可估算余下天数的情景花费。')
        else:
            projection['gap'] = '最近7个完整日的花费或统一币种不足，无法按当前速度估算。'
        packet['spend_projection'] = projection
        packet['limitations'].append('未提供预算上限，不能据此认定超预算；区间不是置信区间。')
    if re.search(r'库存|补货|断货', question or ''):
        packet['limitations'].append('未提供可售库存、在途、补货交期，不能估算断货或补货数量。')
    if re.search(r'搜索词|关键词|哪个词|词浪费', question or ''):
        packet['limitations'].append('本次未提供完整搜索词与关键词报表，不能据活动汇总定位具体浪费词。')
    if re.search(r'小时|分时|时段|几点', question or ''):
        packet['limitations'].append('本次未提供全店完整小时明细，不能用日报推断最佳投放时段。')
    return packet
