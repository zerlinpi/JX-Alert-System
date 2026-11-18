from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.notifications.ad_repeat import (
    INCIDENT, campaign_identity, incident_already_recorded, incident_metadata,
    load_incident_history, repeat_eligible_signals,
)
from jx_alert.storage.db import Base
from jx_alert.storage.models import SyncJob


CLOCK = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)


def _evidence(day, mature_day, spend, **overrides):
    return {'store_id': '5174', 'campaign_id': 'seven', 'ad_type': 'sp',
            'report_date': day, 'mature_date': mature_day, 'currency': 'USD',
            'mature': {'orders': 0, 'spend': spend}, **overrides}


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
        db.add(SyncJob(job_type=INCIDENT, status='HELD',
            started_at=(CLOCK - timedelta(days=4)).replace(tzinfo=None), metadata_json=latest))
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(hours=3)).replace(tzinfo=None),
            metadata_json={**latest, 'origin': 'daily_report'}))
        db.add(SyncJob(job_type=INCIDENT, status='QUEUED',
            started_at=(CLOCK - timedelta(days=7)).replace(tzinfo=None),
            metadata_json={**latest, 'campaign_id': 'outside'}))
        db.add(SyncJob(job_type=INCIDENT, status='HELD',
            started_at=(CLOCK - timedelta(days=3)).replace(tzinfo=None),
            metadata_json={**latest, 'campaign_id': 'not_released', 'release_day': '2026-11-17'}))
        db.commit()
        assert incident_already_recorded(db, start)
        assert not incident_already_recorded(db, current)
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
