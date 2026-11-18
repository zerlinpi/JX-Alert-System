"""Natural ad questions reach the model with only scoped mature evidence."""
# ruff: noqa: F811 - pytest reuses the imported warehouse fixture.
from copy import deepcopy
from datetime import timedelta
import json

import httpx
import pytest
from sqlalchemy import select, func

from test_bot_service import DAY, NOW, enqueue, event, process, warehouse  # noqa: F401
from jx_alert.ai.ad_review import parse_findings, review_advertising, review_cache_key, review_cards
from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, RawSnapshot, SyncJob


QUESTION = '有哪些店铺的广告情况存在异常情况，只发送当前能看见的数据的店铺的异常广告记录，数据不全，或者缺少成熟归因数据的内容不显示'


def ready(warehouse):
    with warehouse[1]() as db:
        for row in db.scalars(select(DailyMetric).where(DailyMetric.dimension == 'campaign_sp')):
            row.source = 'lingxing_ads_sp'
            if row.metric_date == DAY - timedelta(days=7):
                row.value = {'spend': 200, 'orders': 0, 'units': 0, 'sales': 0,
                             'clicks': 60, 'impressions': 4000}[row.metric_name]
        for cov in db.scalars(select(DataCoverage)):
            cov.completed_at = NOW.replace(tzinfo=None)
        for campaign in db.scalars(select(Campaign)):
            campaign.updated_at = NOW.replace(tzinfo=None)
        by_day = {}
        for row in db.scalars(select(DailyMetric).where(DailyMetric.dimension == 'campaign_sp')):
            entry = by_day.setdefault(row.metric_date, {'campaign_id': row.entity_id})
            entry['cost' if row.metric_name == 'spend' else row.metric_name] = row.value
        for day, item in by_day.items():
            db.add(RawSnapshot(source='lingxing_ads_sp_campaign_report', store_id='5174',
                captured_at=NOW.replace(tzinfo=None), payload={
                    'report_type': 'sp', 'report_date': day.isoformat(), 'rows': [item]}))
        db.commit()


class Analyst:
    calls = []
    last_usage = {'prompt_tokens': 100, 'completion_tokens': 20}

    def analyze(self, context, question):
        self.calls.append((deepcopy(context), question))
        row = context['campaigns'][0]
        return json.dumps({'findings': [{'evidence_id': row['evidence_id'], 'signals': ['zero_conversion'],
            'analysis': '成熟点击已积累但没有带来归因订单，优先复核这项活动的流量质量。'}]}, ensure_ascii=False)


def test_exact_operator_prompt_reaches_model_and_omits_every_ineligible_store(warehouse):
    ready(warehouse)
    Analyst.calls = []
    enqueue(warehouse, event('@_user_1 ' + QUESTION))
    result = process(warehouse, factory=lambda _: Analyst())
    assert result['status'] == 'QUEUED' and len(Analyst.calls) == 1
    context, question = Analyst.calls[0]
    assert question == QUESTION and context['ad_review'] is True
    assert {r['store_id'] for r in context['campaigns']} == {'5174'}
    assert {r['ad_type'] for r in context['campaigns']} == {'sp'}
    assert context['campaigns'][0]['mature']['orders'] == 0
    assert 'evidence_dates' not in context['campaigns'][0]
    with warehouse[1]() as db:
        payload = json.dumps([r.payload for r in db.scalars(select(NotificationOutbox))], ensure_ascii=False)
        assert '成熟点击已积累' in payload and '200.00' in payload
        assert all(t not in payload for t in ('数据不全', '缺少成熟', '待补齐', '绩效', '品牌推广', '展示推广', '范围外店铺'))
        assert db.get(SyncJob, result['job_id']).metadata_json['analysis_provider'] == 'deepseek_ad_review'
        assert db.scalar(select(func.count()).select_from(SyncJob).where(SyncJob.job_type == 'manual_collection')) == 0


def test_identical_evidence_and_question_reuses_analysis_without_repeating_model_call(warehouse):
    ready(warehouse)
    Analyst.calls = []
    for index in range(2):
        enqueue(warehouse, event('@_user_1 ' + QUESTION, message_id=f'om_review_{index}'))
        process(warehouse, factory=lambda _: Analyst())
    assert len(Analyst.calls) == 1


def test_scope_revoked_during_model_call_is_not_delivered_as_an_ad_finding(warehouse):
    ready(warehouse)
    class Revoking(Analyst):
        def analyze(self, context, question):
            with warehouse[1]() as db:
                row = db.scalar(select(DataCoverage).where(DataCoverage.dimension == 'campaign_sp', DataCoverage.metric_date == DAY))
                row.status = 'FAILED'
                db.commit()
            return super().analyze(context, question)
    enqueue(warehouse, event('@_user_1 ' + QUESTION))
    result = process(warehouse, factory=lambda _: Revoking())
    with warehouse[1]() as db:
        job = db.get(SyncJob, result['job_id'])
        assert job.metadata_json['analysis_provider'] == 'unavailable'
        assert '广告异常' not in json.dumps([r.payload for r in db.scalars(select(NotificationOutbox))], ensure_ascii=False)


def test_no_mature_evidence_gives_no_missing_data_list_or_model_normality_claim(warehouse):
    enqueue(warehouse, event('@_user_1 ' + QUESTION))
    process(warehouse, factory=lambda _: pytest.fail('No eligible data cannot invoke the model'))
    with warehouse[1]() as db:
        payload = json.dumps([r.payload for r in db.scalars(select(NotificationOutbox))], ensure_ascii=False)
        assert '没有需列出的异常广告记录' in payload
        assert all(t not in payload for t in ('待补齐', '数据不足', '缺少成熟', '广告正常', '绩效'))


def row(index=0):
    return {'evidence_id': str(index), 'material_signals': ['zero_conversion']}


@pytest.mark.parametrize('change', [
    {'evidence_id': 'not-visible'}, {'signals': ['CPC']}, {'signals': []},
    {'analysis': '广告表现正常，可以放心。'}, {'analysis': '数据不足，应补齐后再判断。'},
    {'analysis': '花费三百美元可能过高。'}, {'analysis': '花费300美元。'},
    {'analysis': '通知已发送。'}, {'analysis': 'This is bad.'},
    {'analysis': '已确认库存断货，搜索词浪费已经查明。'},
    {'analysis': '建议立即暂停所有广告。'}, {'analysis': '当前仍在持续烧钱。'},
])
def test_model_cannot_invent_objects_signals_amounts_or_template_gaps(change):
    finding = {'evidence_id': '0', 'signals': ['zero_conversion'], 'analysis': '成熟点击没有带来归因订单。'} | change
    with pytest.raises(DeepSeekError):
        parse_findings(json.dumps({'findings': [finding]}), [row()])


def test_every_candidate_is_reviewed_in_bounded_batches_without_top_n_truncation():
    class All:
        calls = []
        def analyze(self, context, question):
            self.calls.extend(r['evidence_id'] for r in context['campaigns'])
            return '{"findings":[]}'
    client = All()
    context = {'campaigns': [row(i) | {'proof': 'x' * 6000} for i in range(25)]}
    assert len(json.dumps(context)) > 128 * 1024
    key = review_cache_key(context, QUESTION, 'deepseek-flash')
    assert key == review_cache_key(deepcopy(context), QUESTION, 'deepseek-flash')
    changed = deepcopy(context)
    changed['campaigns'][-1]['proof'] += 'changed'
    assert key != review_cache_key(changed, QUESTION, 'deepseek-flash')
    findings, audit = review_advertising(client, context, QUESTION)
    assert findings == [] and audit['batches'] == 3
    assert client.calls == [str(i) for i in range(25)]


def test_review_json_empty_result_and_original_question_use_read_only_official_model_request(monkeypatch):
    original = httpx.Client
    def handler(request):
        payload = json.loads(request.content)
        assert '不机械照抄' in payload['messages'][0]['content'] or '不能机械照抄' in payload['messages'][0]['content']
        assert json.loads(payload['messages'][1]['content'])['question'] == QUESTION
        assert payload['tool_choice'] == 'none' and 'tools' not in payload
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '{"findings":[]}'}}]})
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = DeepSeekClient('synthetic-key').analyze({'ad_review': True, 'campaigns': [row()]}, QUESTION)
    assert parse_findings(result, [row()]) == []
    assert review_cards([])[0]['header']['title']['content'] == '广告查询'
