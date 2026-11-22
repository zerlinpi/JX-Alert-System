from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.notifications.ad_repeat import (
    INCIDENT, campaign_identity, incident_already_recorded, incident_metadata,
    load_incident_history, repeat_eligible_signals,
)
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, Store, SyncJob


CLOCK = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)


def _evidence(day, mature_day, spend, **overrides):
    return {'store_id': '5174', 'campaign_id': 'seven', 'ad_type': 'sp',
            'report_date': day, 'mature_date': mature_day, 'currency': 'USD',
            'mature': {'orders': 0, 'spend': spend}, **overrides}


def _intraday(day='2026-11-19', *, spend=0, clicks=0, impressions=20000):
    return _evidence(day, None, 0, intraday=True, verified_daily_budget=10,
        current={'spend': spend, 'clicks': clicks, 'impressions': impressions})


def _outbox(db, label, status, destination='chat_id', **payload):
    row = NotificationOutbox(idempotency_key=label, status=status,
        payload={'delivery': {'receive_id_type': destination}, **payload})
    db.add(row)
    db.flush()
    return row.id


def test_steady_incident_needs_fresh_material_worsening_after_cooldown():
    start = _evidence('2026-11-14', '2026-11-07', 80)
    later = _evidence('2026-11-18', '2026-11-11', 112)
    history = {campaign_identity(start): [(CLOCK, incident_metadata(start, ['zero_conversion']))]}
    now = CLOCK + timedelta(days=3)
    assert repeat_eligible_signals(later, ['zero_conversion', 'CPC'], history, now - timedelta(seconds=1)) == []
    assert repeat_eligible_signals(later, ['zero_conversion'], history, now) == ['zero_conversion']
    assert repeat_eligible_signals({**later, 'mature': {'orders': 0, 'spend': 100}},
        ['zero_conversion'], history, now) == []
    assert repeat_eligible_signals({**later, 'mature_date': start['mature_date']},
        ['zero_conversion'], history, now) == []
    assert repeat_eligible_signals(later, ['orders', 'CPC'], history, now) == ['CPC']
    assert repeat_eligible_signals({**later, 'currency': 'CAD'}, ['zero_conversion'], history, now) == []


def test_recent_history_includes_held_and_daily_reservations_and_has_a_seven_day_boundary():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    current = _evidence('2026-11-18', '2026-11-11', 90)
    start = _evidence('2026-11-13', '2026-11-06', 80)
    with Session(engine) as db:
        latest = incident_metadata(start, ['zero_conversion'])
        group = _outbox(db, 'daily-report', 'PENDING')
        db.add(SyncJob(job_type=INCIDENT, status='HELD',
            started_at=(CLOCK - timedelta(days=4)).replace(tzinfo=None),
            metadata_json={**latest, 'release_day': '2026-11-19'}))
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(hours=3)).replace(tzinfo=None),
            metadata_json={**latest, 'origin': 'daily_report', 'outbox_ids': [group]}))
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(days=7)).replace(tzinfo=None),
            metadata_json={**latest, 'campaign_id': 'outside', 'outbox_ids': [group]}))
        db.add(SyncJob(job_type=INCIDENT, status='HELD',
            started_at=(CLOCK - timedelta(days=3)).replace(tzinfo=None),
            metadata_json={**latest, 'campaign_id': 'not_released', 'release_day': '2026-11-17'}))
        db.commit()
        assert incident_already_recorded(db, start, now=CLOCK)
        assert not incident_already_recorded(db, current, now=CLOCK)
        history = load_incident_history(db, CLOCK)
        assert [m.get('origin') for _, m in history[campaign_identity(current)]] == ['daily_report', None]
        assert repeat_eligible_signals(current, ['zero_conversion'], history, CLOCK) == []
        assert repeat_eligible_signals({**current, 'campaign_id': 'outside'},
            ['zero_conversion'], history, CLOCK) == ['zero_conversion']
        assert repeat_eligible_signals({**current, 'campaign_id': 'not_released'},
            ['zero_conversion'], history, CLOCK) == ['zero_conversion']
        assert repeat_eligible_signals({**current, 'store_id': 'another'},
            ['zero_conversion'], history, CLOCK) == ['zero_conversion']
    engine.dispose()


@pytest.mark.parametrize(('status', 'receipt', 'reserved'), [
    ('PENDING', None, True), ('RETRY', None, True), ('SENDING', None, True),
    ('DEAD', None, False), ('SKIPPED', None, False), ('EXPIRED', None, False),
    ('DELIVERED', None, True),    # Acknowledged legacy per-campaign card.
    ('DELIVERED', 'included', True),
    ('DELIVERED', 'filtered_out', False),
    ('DELIVERED', 'different_store', False),
])
def test_repeat_reservation_follows_group_delivery_and_its_visible_activities(status, receipt, reserved):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    current = _evidence('2026-11-18', '2026-11-11', 90)
    incident = incident_metadata(current, ['zero_conversion'])
    receipt_data = {}
    if receipt is not None:
        receipt_data['hourly_delivered_items'] = [{
            'store_id': '5174' if receipt != 'different_store' else '5175', 'ad_type': 'sp',
            'campaign_id': 'seven' if receipt != 'filtered_out' else 'another',
        }]
    with Session(engine) as db:
        group = _outbox(db, 'group', status, **receipt_data)
        lead = _outbox(db, 'lead', 'DELIVERED', destination='open_id')
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(hours=1)).replace(tzinfo=None),
            metadata_json={**incident, 'outbox_ids': [group, lead]}))
        db.commit()
        assert incident_already_recorded(db, current, now=CLOCK) is reserved
        recent = load_incident_history(db, CLOCK)
        assert (campaign_identity(current) in recent) is reserved
        assert repeat_eligible_signals(current, ['zero_conversion'], recent, CLOCK) == (
            [] if reserved else ['zero_conversion'])
    engine.dispose()


def test_exact_incident_releases_expired_holds_and_unrouted_reservations():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    row = _evidence('2026-11-18', '2026-11-11', 90)
    incident = incident_metadata(row, ['zero_conversion'])
    with Session(engine) as db:
        db.add(SyncJob(job_type=INCIDENT, status='HELD',
            started_at=(CLOCK - timedelta(days=2)).replace(tzinfo=None),
            metadata_json={**incident, 'release_day': '2026-11-18'}))
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(days=1)).replace(tzinfo=None), metadata_json=incident))
        db.commit()
        assert incident_already_recorded(db, row, now=CLOCK - timedelta(days=1))
        assert not incident_already_recorded(db, row, now=CLOCK)
        assert campaign_identity(row) not in load_incident_history(db, CLOCK)
    engine.dispose()


def test_repeat_kpi_requires_same_comparison_basis_and_both_worsening_dimensions():
    start = _evidence('2026-11-14', '2026-11-07', 80, kpis={'CPC': {
        'current': 1.5, 'baseline': 1, 'current_date': '2026-11-14',
        'comparison_basis': 'SEVEN_DAY',
    }})
    history = {campaign_identity(start): [(CLOCK, incident_metadata(start, ['CPC']))]}
    later = _evidence('2026-11-19', '2026-11-12', 80)
    now = CLOCK + timedelta(days=4)
    kpi = {'current': 2.1, 'baseline': 1.2, 'current_date': '2026-11-19', 'comparison_basis': 'SEVEN_DAY'}
    assert repeat_eligible_signals({**later, 'kpis': {'CPC': kpi}}, ['CPC'], history, now) == ['CPC']
    for change in ({'comparison_basis': 'PREVIOUS_DAY'}, {'current': 1.9},
                   {'baseline': 3, 'current': 4}, {'current_date': '2026-11-14'}):
        assert repeat_eligible_signals({**later, 'kpis': {'CPC': {**kpi, **change}}},
            ['CPC'], history, now) == []


@pytest.mark.parametrize('status', ('PENDING', 'DELIVERED'))
def test_completed_day_group_reservation_does_not_suppress_new_intraday_risk(status):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    older = _evidence('2026-11-18', '2026-11-11', 90)
    traffic, budget = _intraday(), _intraday(spend=60, clicks=11)
    with Session(engine) as db:
        incident = incident_metadata(older, ['zero_conversion'])
        incident.pop('intraday')  # Older valid receipts do not have this discriminator.
        group = _outbox(db, 'daily', status)
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(hours=1)).replace(tzinfo=None),
            metadata_json={**incident, 'outbox_ids': [group]}))
        db.commit()
        history = load_incident_history(db, CLOCK)
        assert repeat_eligible_signals(traffic, ['intraday_no_clicks', 'CTR'], history, CLOCK) == ['intraday_no_clicks']
        assert repeat_eligible_signals(budget, ['intraday_budget', 'CPC'], history, CLOCK) == ['intraday_budget']
        assert repeat_eligible_signals(older, ['zero_conversion'], history, CLOCK) == []
    engine.dispose()


@pytest.mark.parametrize('previous_kind', ('intraday_budget', 'intraday_no_clicks', 'unknown_legacy'))
def test_intraday_cooldown_checks_all_recent_group_reservations(previous_kind):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    older = _evidence('2026-11-18', '2026-11-11', 90)
    prior_intraday = _intraday('2026-11-18', spend=60 if previous_kind == 'intraday_budget' else 0)
    if previous_kind == 'unknown_legacy':
        previous = {key: prior_intraday[key] for key in ('store_id', 'campaign_id', 'ad_type', 'report_date')}
    else:
        previous = incident_metadata(prior_intraday, [previous_kind])
    with Session(engine) as db:
        for hours, metadata in ((1, incident_metadata(older, ['zero_conversion'])), (2, previous)):
            group = _outbox(db, f'group-{hours}', 'DELIVERED')
            db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
                started_at=(CLOCK - timedelta(hours=hours)).replace(tzinfo=None),
                metadata_json={**metadata, 'outbox_ids': [group]}))
        db.commit()
        history = load_incident_history(db, CLOCK)
        assert repeat_eligible_signals(_intraday(), ['intraday_no_clicks'], history, CLOCK) == []
        assert repeat_eligible_signals(_intraday(spend=60), ['intraday_budget'], history, CLOCK) == []
    engine.dispose()


def test_repeat_zero_clicks_requires_new_day_and_materially_more_impressions():
    prior = _intraday('2026-11-14', impressions=50000)
    signals = ['intraday_no_clicks']
    meta = incident_metadata(prior, signals)
    assert meta['risk_metrics']['intraday_no_clicks'] == (
        'intraday_zero_click_impressions', '2026-11-14', 50000, None)
    history = {campaign_identity(prior): [(CLOCK, meta)]}
    now = CLOCK + timedelta(days=3)
    assert not repeat_eligible_signals(_intraday(impressions=61000), signals, history, now)
    assert not repeat_eligible_signals(_intraday('2026-11-14', impressions=63000), signals, history, now)
    assert not repeat_eligible_signals(_intraday(spend=10, impressions=63000), signals, history, now)
    assert repeat_eligible_signals(_intraday(impressions=63000), signals, history, now) == signals


@pytest.mark.parametrize(("age", "day", "budget", "zone", "reserved"), [
    (180, "2026-11-21", 10, "America/Los_Angeles", False),
    (75, "2026-11-21", 10, "America/Los_Angeles", True),
    (20, "2026-11-20", 10, "America/Los_Angeles", False),
    (20, "2026-11-21", 11, "America/Los_Angeles", False),
    (20, "2026-11-21", 10, None, False),
    (20, "2026-11-21", 10, "Unknown/Timezone", False),
    (-1, "2026-11-21", 10, "America/Los_Angeles", False),
])
def test_held_live_incident_reserves_only_while_it_can_still_be_delivered(age, day, budget, zone, reserved):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    open_at = datetime(2026, 11, 22, 0, tzinfo=timezone.utc)  # 08:00 Beijing; still Nov 21 in LA
    signal = ["intraday_budget"]
    fresh = _intraday("2026-11-21", spend=100, clicks=11)
    capture = open_at - timedelta(minutes=age)
    with Session(engine) as db:
        db.add(Store(sid="5174", name="GETEEN", timezone=zone, status=1))
        db.add(Campaign(store_id="5174", campaign_type="sp", campaign_id="seven", state="enabled", budget=10))
        db.add(SyncJob(job_type=INCIDENT, status="HELD",
            started_at=(open_at - timedelta(minutes=70)).replace(tzinfo=None),
            metadata_json={**incident_metadata(fresh, signal), "release_day": "2026-11-22",
                "hourly_item": {"live_observation": {
                    "day": day, "captured_at": capture.isoformat(), "daily_budget": budget}}}))
        db.commit()

        assert incident_already_recorded(db, fresh, now=open_at) is reserved
        history = load_incident_history(db, open_at)
        assert (campaign_identity(fresh) in history) is reserved
        assert repeat_eligible_signals(fresh, signal, history, open_at) == ([] if reserved else signal)
    engine.dispose()
