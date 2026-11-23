"""Require independent mature data before deriving low-spend or high-spend patterns."""
from datetime import date, timedelta
import json

import pytest

from jx_alert.ai.ad_review import parse_findings
from jx_alert.ai.ad_review_evidence import automatic_sp_signals
from jx_alert.ai.ad_review_skills import DEFAULT_SKILL
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.analysis.hourly_ad_patterns import HIGH_LOW, LOW_ZERO, OPPORTUNITY, classify_hourly_ad_pattern
from jx_alert.notifications.ad_repeat import incident_key, incident_metadata, repeat_eligible_signals
from jx_alert.notifications.hourly_ads import advertising_card, alert_item


DAY = date(2026, 11, 10)
OLDER = [DAY - timedelta(days=n) for n in range(9, 2, -1)]
LATEST = [DAY - timedelta(days=n) for n in (2, 1, 0)]


def _row(spend, clicks, orders, sales, **other):
    return dict(currency='USD', sales_currency='USD', spend=spend, clicks=clicks, orders=orders,
                sales=sales, units=orders, **other)


def _data(recent):
    return {**dict.fromkeys(OLDER, _row(60, 40, 4, 120)), **dict(zip(LATEST, recent))}


def _judge(data, *, skill=DEFAULT_SKILL):
    return classify_hourly_ad_pattern(data, DAY, OLDER, skill)


@pytest.mark.parametrize(('recent', 'expected'), [
    ([_row(32, 28, 0, 0)] * 3, LOW_ZERO),
    ([_row(15, 25, 0, 0)] * 3, None),  # low accumulated spend, even with enough clicks
    ([_row(30, 8, 0, 0)] * 3, None),  # insufficient clicks
    ([_row(32, 28, 0, 0), None, _row(32, 28, 0, 0)], None),
    ([_row(32, 28, 0, 0), _row(32, 28, 0, 20), _row(32, 28, 0, 0)], None),
    ([_row(20, 15, 3, 90)] * 3, OPPORTUNITY),
    ([_row(20, 15, 1, 90)] * 3, None),  # little conversion volume even on high sales
    ([_row(60, 40, 2, 70)] * 2 + [_row(140, 100, 3, 70)], HIGH_LOW),
    ([_row(60, 40, 2, 70)] * 2 + [_row(140, 100, 9, 250)], None),  # spending to scale
    ([_row(60, 40, 2, 70)] * 2 + [_row(55, 100, 3, 70)], None),  # not an economic impact
])
def test_pattern_uses_volume_value_and_confirmed_source(recent, expected):
    proof = _judge(_data(recent))
    if expected is None:
        assert proof is None
    else:
        assert proof['code'] == expected
        assert {d.isoformat() for d in OLDER}.isdisjoint(proof['dates'])


def test_no_low_spend_pattern_if_reference_missing_or_currency_is_unconfirmed():
    spends = _data([_row(32, 28, 0, 0)] * 3)
    missing = spends.pop(OLDER[2])
    assert _judge(spends) is None
    spends[OLDER[2]] = {**missing, 'currency': None}
    assert _judge(spends) is None
    spends[OLDER[2]] = missing
    assert _judge(spends, skill={**DEFAULT_SKILL, 'min_spend_usd': 150}) is None


def _evidence(code, recent):
    return {'evidence_id': 'source-bound-1', 'store_id': '5174', 'ad_type': 'sp',
            'campaign_id': 'c-1', 'campaign_name': 'Campaign A', 'store_name': '店一',
            'report_date': '2026-11-17', 'mature_date': DAY.isoformat(), 'attribution_days': 7,
            'source': 'lingxing_ads_sp', 'currency': 'USD', 'current': {}, 'mature': {},
            'material_signals': [code], 'kpis': {}, 'hourly_pattern': _judge(_data(recent))}


def test_opportunity_stays_separate_from_risk_in_reviews_cards_and_cooldown():
    evidence = _evidence(OPPORTUNITY, [_row(20, 15, 3, 90)] * 3)
    assert automatic_sp_signals(evidence) == [OPPORTUNITY]
    assert automatic_sp_signals({**evidence, 'material_signals': ['CPC']}) == ['CPC']
    reply = json.dumps({'findings': [{'evidence_id': evidence['evidence_id'], 'signals': [OPPORTUNITY]}]})
    finding, = parse_findings(reply, [evidence], selection_only=True)
    with pytest.raises(DeepSeekError):
        parse_findings(reply, [evidence])
    item = alert_item(finding, include_dates=True)
    payload = advertising_card([item])
    assert payload['header']['template'] == 'blue'
    assert payload['header']['title']['content'] == '广告优化机会'
    text = json.dumps(payload, ensure_ascii=False)
    assert all(label in text for label in ('优化机会 · 非浪费告警', '2026-11-08 至 2026-11-10', '2026-11-01 至 2026-11-07', '归因订单 9'))
    assert incident_key(evidence) != incident_key({**evidence, 'category': 'opportunity'})
    old = incident_metadata({**evidence, 'category': 'opportunity'}, [OPPORTUNITY])
    from datetime import datetime, timezone
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    history = {('5174', 'sp', 'c-1'): [(now - timedelta(hours=1), old)]}
    assert repeat_eligible_signals(evidence, ['zero_conversion'], history, now) == ['zero_conversion']
    assert repeat_eligible_signals(evidence, [OPPORTUNITY], history, now) == []
