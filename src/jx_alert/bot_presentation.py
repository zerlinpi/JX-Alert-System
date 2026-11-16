"""Operator-facing answers: narrow tables, health first, no empty data dumps."""
import re

from jx_alert.notifications.cards import card, number, compact_ad_kpi_rows


def general_context(snapshots):
    return {'reply_mode': 'answer', 'query_scope': '仅以下已授权店铺的已有绩效观测；未提供广告金额或订单事实时不得猜测',
        'stores': [{k: s.get(k) for k in ('store_name', 'country', 'ahr_score', 'source_status_label',
            'status_label', 'confirmation_reason', 'source_updated_at', 'pending_notice_observations')}
            for s in snapshots],
        'capabilities': ['按店铺全名或片段查询已保存数据', '列出当前店铺数据', '解释广告指标和绩效观测',
            '负责人或管理员明确请求后可排队同步最新已结束日报；口头回答不会执行同步',
            '没有广告修改能力；通知发送须明确请求并通过原有通道，不宣称本次已经发送']}


def reply_mode(question):
    if re.search(r'表格|日报|快照|完整数据|全部指标', question or ''):
        return 'tables'
    if re.search(r'为什么|为何|怎么|如何|建议|解释|是否|有没有|正常吗|只回答|只告诉|只要|多少|几单|简短|一句话|不要表格|不用表格|只列|什么意思', question or ''):
        return 'answer'
    return 'tables' if re.search(r'情况|概况|数据|指标|表现|经营', question or '') else 'answer'


def operator_card(context):
    sections, notes = [], []
    health = context.get('health')
    if health:
        rows = [('健康评分', number(health['ahr_score']), health['source_status_label'] + '（源观测）'),
                ('当前判定', health['status_label'], health['confirmation_reason']),
                ('待处理通知', str(health['pending_notice_observations']), '归档观测，不能证明当前无违规')]
        rows.extend((r['label'], f"{r['value']:g}%", f"标准{r['standard']:g}%；{r['status_label']}")
            for r in health['indicators'] if r['value'] is not None and r['standard'] is not None)
        sections.append(('店铺绩效快报', ['指标', '观测', '依据'], rows))
        notes.append('绩效源时间：' + (health['source_updated_at'] or '未提供') + '；' + health['freshness_label'])
    metrics = context.get('sales_and_spend', [])
    known = [r for r in metrics if not str(r['current_display']).startswith(('未知', '接口无记录', '—'))]
    if known:
        sections.append(('经营与广告', ['指标', '报表日', '前一日/变化'], [
            (r['metric'], r['current_display'], r['previous_display'] + ' / ' + r['comparison_display']) for r in known]))
    kpis = context.get('advertising_kpis', {})
    rows = compact_ad_kpi_rows(kpis)
    if rows:
        sections.append(('广告指标 · 转化指标采用成熟日', ['指标', '本期/日期', '参考/变化', '判断'], rows))
    totals = []
    for r in context.get('advertising_totals', []):
        if all(r.get(k) is None for k in ('spend', 'orders', 'units', 'sales', 'clicks', 'impressions')):
            continue
        totals.extend((r['ad_type'] + ' · ' + label, number(r.get(metric), money=metric in {'spend', 'sales'},
            currency=r.get(metric + '_currency') or '')) for label, metric in (
                ('花费', 'spend'), ('归因订单', 'orders'), ('归因销量', 'units'), ('归因销售额', 'sales'), ('点击', 'clicks'), ('曝光', 'impressions')))
    if totals:
        sections.append(('广告明细', ['指标', '数值'], totals))
    gaps = [r['dataset'] + '：' + r['status_label'] for r in context.get('coverage', []) if r['status_label'] != '采集完成']
    if gaps:
        notes.append('数据待确认：' + '；'.join(gaps))
    for key in ('data_warning', 'authorization_warning', 'report_date_warning'):
        if context.get(key):
            notes.append(context[key])
    if context.get('question_topic') != 'health':
        notes.insert(0, '实际报表日：' + context['report_date'] + '；前一日：' + metrics[0]['previous_date'] if metrics
            else '实际报表日：' + context['report_date'])
    return card('店铺日报' if context.get('reply_mode') == 'tables' else '店铺数据', subtitle=context['store_name'],
        sections=sections, notes=list(dict.fromkeys(notes)))
