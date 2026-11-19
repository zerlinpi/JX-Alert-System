"""Operator-facing answers: narrow tables, health first, no empty data dumps."""
import html
import re
import math

from jx_alert.notifications.cards import card, number, compact_ad_kpi_rows, has_quantitative_text, table
from jx_alert.notifications.store_health import card_fits


def _model_table(lines, offset):
    def cells(line):
        return [item.strip() for item in re.split(r'(?<!\\)\|', line.strip().strip('|'))]

    if offset + 2 >= len(lines) or '|' not in lines[offset]:
        return None
    headers, divider = cells(lines[offset]), cells(lines[offset + 1])
    if not (2 <= len(headers) == len(divider) <= 4 and all(headers)
            and all(re.fullmatch(r':?-{3,}:?', cell) for cell in divider)):
        return None
    rows = []
    for line in lines[offset + 2:]:
        row = cells(line)
        if len(row) != len(headers) or not all(len(cell) <= 500 for cell in row):
            break
        rows.append(row)
    if rows:
        return headers, rows
    return None


def analysis_cards(title, analysis, *, subtitle=''):
    """Use native, inert table cells for data even when the model sends prose."""
    lines = [line.strip() for line in analysis.splitlines() if line.strip()]
    elements, data = [], []
    offset = 0
    while offset < len(lines):
        parsed = _model_table(lines, offset)
        line = lines[offset]
        if not parsed and (has_quantitative_text(line) or '|' in line):
            data.extend((line[i:i + 500],) for i in range(0, len(line), 500))
            offset += 1
            continue
        if data:
            elements.extend(table(['数据与依据'], data[i:i + 10]) for i in range(0, len(data), 10))
            data.clear()
        if parsed:
            headers, rows = parsed
            elements.extend(table(headers, rows[i:i + 3]) for i in range(0, len(rows), 3))
            offset += len(rows) + 2
        else:
            elements.extend({'tag': 'markdown', 'content': html.escape(line[i:i + 1200], quote=False)}
                            for i in range(0, len(line), 1200))
            offset += 1
    elements.extend(table(['数据与依据'], data[i:i + 10]) for i in range(0, len(data), 10))
    cards = [card(title, subtitle=subtitle, sections=[])]
    for element in elements:
        contents = cards[-1]['body']['elements']
        contents.append(element)
        if not card_fits(cards[-1]):
            contents.pop()
            value = card(title, subtitle=subtitle, sections=[])
            value['body']['elements'].append(element)
            cards.append(value)
    return cards


def general_context(snapshots):
    return {'reply_mode': 'answer', 'query_scope': '仅以下已授权店铺的已有绩效观测；未提供广告金额或订单事实时不得猜测',
        'stores': [{k: s.get(k) for k in ('store_name', 'country', 'ahr_score', 'source_status_label',
            'status_label', 'confirmation_reason', 'source_updated_at', 'pending_notice_observations')}
            for s in snapshots],
        'capabilities': ['按店铺全名或片段查询已保存数据', '列出当前店铺数据', '解释广告指标和绩效观测',
            '分析完整历史日报的趋势与情景预估；负责人可私聊明确请求刷新，今日采集单独作为盘中观测',
            '没有广告修改能力；通知发送须明确请求并通过原有通道，不宣称本次已经发送']}


def reply_mode(question):
    if re.search(r'表格|日报|快照|完整数据|全部指标', question or ''):
        return 'tables'
    if re.search(r'为什么|为何|怎么|如何|建议|解释|是否|有没有|正常吗|只回答|只告诉|只要|多少|几单|简短|一句话|不要表格|不用表格|只列|什么意思|预估|预测|预判|拆解|下钻|浪费|趋势', question or ''):
        return 'answer'
    return 'tables' if re.search(r'情况|概况|数据|指标|表现|经营', question or '') else 'answer'


def operator_card(context):
    sections, notes = [], []
    health = context.get('health')
    if health:
        rows = []
        score = health.get('ahr_score')
        source = health.get('source_status_label')
        unavailable = health.get('status') in {'AUTHORIZATION_FAILED', 'INACTIVE', 'NO_DATA'}
        known_source = source not in {None, '', '未知', '未提供', '未识别'}
        if not unavailable and isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            rows.append(('健康评分', number(score), (source if known_source else '') + '（归档观测）'))
        elif not unavailable and known_source:
            rows.append(('评级', source, '归档观测'))
        reason = health.get('confirmation_reason') or health.get('status_label')
        reason = {'AUTHORIZATION_FAILED': '领星授权失败，请恢复授权后同步',
                  'NO_DATA': '暂无绩效记录，请同步绩效数据',
                  'INACTIVE': '店铺未启用，最新绩效待确认'}.get(health.get('status'), reason)
        if unavailable and reason:
            rows.append(('关注项', reason, ''))
        if health.get('pending_notice_observations', 0) > 0:
            rows.append(('归档待处理通知', str(health['pending_notice_observations']), '请核对亚马逊最新通知'))
        rows.extend((r['label'], f"{r['value']:g}%", f"标准{r['standard']:g}%；{r['status_label']}")
            for r in health.get('indicators', []) if r.get('status') in {'BREACHED', 'AT_THRESHOLD'}
            and r.get('value') is not None and r.get('standard') is not None)
        if not rows:
            rows.append(('绩效', '尚无可用观测，请同步绩效数据', ''))
        sections.append(('店铺绩效快报', ['指标', '观测', '依据'], rows))
        if not unavailable and health.get('freshness_status') == 'FRESH' and health.get('source_updated_at'):
            notes.append('绩效源时间：' + health['source_updated_at'])
    metrics = context.get('sales_and_spend', [])
    known = [r for r in metrics if not str(r['current_display']).startswith(('未知', '接口无记录', '—'))]
    if known:
        def reference(item):
            prior = item['previous_display']
            delta = item['comparison_display']
            if str(prior).startswith(('未知', '接口无记录', '—')):
                return ''
            return prior + (' / ' + delta if not str(delta).startswith(('未知', '待', '接口无记录', '币种')) else '')
        sections.append(('经营与广告', ['指标', '报表日', '前一日/变化'], [
            (r['metric'], r['current_display'], reference(r)) for r in known]))
    alternate = context.get('source_observations') or {}
    if not known or context.get('data_warning'):
        confirmed = []
        sales = alternate.get('store_sales') or {}
        for key, label in [('sales', '下单销售额'), ('units', '下单销量'), ('orders', '下单订单')]:
            value = sales.get('metrics', {}).get(key)
            code = sales.get('currency')
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                if key != 'sales' or re.fullmatch(r'[A-Z]{3}', code or ''):
                    confirmed.append((label, number(value, money=key == 'sales', currency=code or '')))
        for item in alternate.get('campaign_ads') or []:
            for key, label in [('spend', '花费'), ('clicks', '点击'), ('impressions', '曝光')]:
                value = item.get('metrics', {}).get(key)
                code = item.get('currency')
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    if key != 'spend' or re.fullmatch(r'[A-Z]{3}', code or ''):
                        confirmed.append((item['ad_type'] + label, number(value, money=key == 'spend', currency=code or '')))
        if confirmed:
            sections.append(('领星MCP独立观测', ['指标', '数值'], confirmed))
            notes.append('下单口径与接口付款口径分开呈现，不据此计算混合来源环比。')
    kpis = {key: item for key, item in context.get('advertising_kpis', {}).items()
        if isinstance(item.get('current'), (int, float)) and not isinstance(item.get('current'), bool)
        and math.isfinite(item['current']) and (item.get('format') != 'money'
            or re.fullmatch(r'[A-Z]{3}', item.get('currency') or ''))}
    rows = compact_ad_kpi_rows(kpis)
    rows = [(row[0], row[1], row[2] if item.get('baseline') is not None else '',
             row[3] if item.get('status') == 'ADVERSE' else '') for row, item in zip(rows, kpis.values())]
    if rows:
        if any(row[2] or row[3] for row in rows):
            sections.append(('广告指标 · 转化指标采用成熟日', ['指标', '本期/日期', '参考/变化', '判断'], rows))
        else:
            sections.append(('广告指标 · 转化指标采用成熟日', ['指标', '本期/日期'], [row[:2] for row in rows]))
    totals = []
    for r in context.get('advertising_totals', []):
        if all(r.get(k) is None for k in ('spend', 'orders', 'units', 'sales', 'clicks', 'impressions')):
            continue
        totals.extend((r['ad_type'] + ' · ' + label, number(r.get(metric), money=metric in {'spend', 'sales'},
            currency=r.get(metric + '_currency') or '')) for label, metric in (
                ('花费', 'spend'), ('归因订单', 'orders'), ('归因销量', 'units'), ('归因销售额', 'sales'), ('点击', 'clicks'), ('曝光', 'impressions'))
            if isinstance(r.get(metric), (int, float)) and not isinstance(r.get(metric), bool)
            and math.isfinite(r[metric])
            and (metric not in {'spend', 'sales'} or re.fullmatch(r'[A-Z]{3}', r.get(metric + '_currency') or '')))
    if totals:
        sections.append(('广告明细', ['指标', '数值'], totals))
    gaps = [r['dataset'] + '：' + r['status_label'] for r in context.get('coverage', []) if r['status_label'] != '采集完成']
    if gaps and not sections:
        notes.append('暂未取得可展示的数据，请先同步该店铺。')
    for key in ('data_warning', 'authorization_warning', 'report_date_warning'):
        if context.get(key):
            notes.append(context[key])
    if context.get('question_topic') != 'health':
        notes.insert(0, '实际报表日：' + context['report_date'] + '；前一日：' + metrics[0]['previous_date'] if metrics
            else '实际报表日：' + context['report_date'])
    return card('店铺日报' if context.get('reply_mode') == 'tables' else '店铺数据', subtitle=context['store_name'],
        sections=sections, notes=list(dict.fromkeys(notes)))


def observation_card(context):
    """Display explicit source observations, never fill unprovided fields."""
    observation = context['intraday_observation']
    rows = []
    for item in observation['campaigns']:
        for field, label in [('cost', '花费'), ('orders', '归因订单观测'), ('clicks', '点击'), ('impressions', '曝光')]:
            value = item.get(field)
            if value is None or (field == 'cost' and not item.get('currency')):
                continue
            rows.append((item['name'], label, number(value, money=field == 'cost', currency=item.get('currency') or '')))
    notes = ['盘中观测，非完整日报；归因订单仍会更新，不据此认定零转化或正常。']
    if observation['campaign_count'] > len(observation['campaigns']):
        notes.append(f"共{observation['campaign_count']}个活动，本消息展示前{len(observation['campaigns'])}个活动。")
    if not rows:
        notes.append('暂未取得可展示的今日广告数值，请稍后重新采集。')
    return card('今日广告观测', subtitle=f"{context['store_name']} · {context['report_date']}",
        sections=[('SP活动', ['活动', '指标', '观测值'], rows)], notes=notes)
