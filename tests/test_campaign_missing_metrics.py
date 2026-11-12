from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from jx_alert.analysis.evaluator import evaluate_daily_anomalies
from jx_alert.analysis.service import upsert_anomalies
from jx_alert.analysis.rules import RuleConfig
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import AnomalyEvent, Campaign, DailyMetric, DataCoverage, Store


TARGET = date(2026, 11, 11)
STORE = "5174"
CAMPAIGN = "141469378857743"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Store(sid=STORE, name="Store", status=1, has_ads_setting=1))
        for offset in range(15):
            session.add(DataCoverage(
                store_id=STORE, metric_date=TARGET - timedelta(days=offset),
                dimension="campaign_sp", status="COMPLETE", row_count=2,
            ))
        _seed_campaign(session, CAMPAIGN)
        session.commit()
        yield session
    engine.dispose()


def _seed_campaign(db, campaign_id):
    db.add(Campaign(
        store_id=STORE, campaign_type="sp", campaign_id=campaign_id,
        name=campaign_id, state="enabled",
    ))
    for offset in range(15):
        for name, value in {"spend": 10, "orders": 2, "clicks": 20}.items():
            db.add(DailyMetric(
                store_id=STORE, metric_date=TARGET - timedelta(days=offset),
                dimension="campaign_sp", entity_id=campaign_id,
                metric_name=name, value=value,
            ))


def _existing_alert(db, campaign_id=CAMPAIGN):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    event = AnomalyEvent(
        dedupe_key=f"{STORE}:{campaign_id}:high_ad_spend", store_id=STORE,
        entity_id=campaign_id, rule="high_ad_spend", severity="P1", status="ONGOING",
        evaluation_scope=f"{STORE}:campaign:sp", metrics={"spend": 30},
        reason="Earlier observed spend", first_seen=now - timedelta(days=1),
        last_seen=now - timedelta(hours=1),
    )
    db.add(event)
    db.commit()
    return event


def _metric(db, campaign_id, field, offset=0):
    return db.scalar(select(DailyMetric).where(
        DailyMetric.entity_id == campaign_id, DailyMetric.metric_name == field,
        DailyMetric.metric_date == TARGET - timedelta(days=offset),
    ))


@pytest.mark.parametrize("field,offset", [
    ("spend", 0), ("spend", 8), ("orders", 7), ("orders", 9), ("clicks", 0),
])
def test_missing_sp_campaign_metric_cannot_create_zero_alert_or_recover_active(db, field, offset):
    existing = _existing_alert(db)
    old_last_seen = existing.last_seen
    db.delete(_metric(db, CAMPAIGN, field, offset))
    db.commit()

    result = evaluate_daily_anomalies(db, {STORE: TARGET.isoformat()}, RuleConfig())

    assert existing.status == "ONGOING"
    assert existing.last_seen == old_last_seen
    assert existing.recovered_at is None
    assert db.scalars(select(AnomalyEvent)).all() == [existing]
    assert db.scalars(select(NotificationOutbox)).all() == []
    assert result["events_touched"] == 0
    assert result["skipped_scope_details"] == [{
        "store_id": STORE, "dimension": "campaign_sp", "campaign_id": CAMPAIGN,
        "reason": "MISSING_CAMPAIGN_METRICS", "missing": [{
            "metric_name": field,
            "metric_date": (TARGET - timedelta(days=offset)).isoformat(),
        }],
    }]


def test_complete_report_without_target_campaign_row_is_unknown(db):
    existing = _existing_alert(db)
    db.execute(delete(DailyMetric).where(
        DailyMetric.entity_id == CAMPAIGN, DailyMetric.metric_date == TARGET,
    ))
    db.commit()

    result = evaluate_daily_anomalies(db, TARGET, RuleConfig())

    assert existing.status == "ONGOING"
    assert result["events_touched"] == 0
    assert result["notifications_queued"] == 0
    assert result["skipped_scope_details"][0]["reason"] == "MISSING_CAMPAIGN_METRICS"


def test_explicit_zero_metrics_remain_valid_and_can_recover_prior_alert(db):
    existing = _existing_alert(db)
    for offset in (0, 7):
        for field in ("spend", "orders", "clicks"):
            _metric(db, CAMPAIGN, field, offset).value = 0
    db.commit()

    result = evaluate_daily_anomalies(db, TARGET, RuleConfig())

    assert result["skipped_scope_details"] == []
    assert existing.status == "RECOVERED"
    zero_alert = db.scalar(select(AnomalyEvent).where(
        AnomalyEvent.rule == "low_spend_no_conversion",
    ))
    # Explicit zeros are valid data, but zero spend/clicks do not meet the
    # minimum sample needed for a conversion warning.
    assert zero_alert is None


def test_missing_campaign_preserves_scope_but_other_complete_campaign_still_alerts(db):
    existing = _existing_alert(db)
    other = "141469378857744"
    _seed_campaign(db, other)
    db.flush()
    _metric(db, other, "spend").value = 30
    db.delete(_metric(db, CAMPAIGN, "spend"))
    db.commit()

    result = evaluate_daily_anomalies(db, TARGET, RuleConfig())

    assert existing.status == "ONGOING"
    assert result["skipped_scope_details"][0]["campaign_id"] == CAMPAIGN
    assert result["events_touched"] == 1
    assert result["notifications_queued"] == 1
    other_alert = db.scalar(select(AnomalyEvent).where(AnomalyEvent.entity_id == other))
    assert other_alert.rule == "high_ad_spend"
    assert other_alert.metrics["spend"] == 30


@pytest.mark.parametrize("missing_state", ["enabled", "archived", "not_returned"])
def test_complete_campaign_recovers_without_recovering_campaign_missing_history(db, missing_state):
    complete_alert = _existing_alert(db, CAMPAIGN)
    missing_campaign = "141469378857744"
    db.add(Campaign(
        store_id=STORE, campaign_type="sp", campaign_id=missing_campaign,
        name=missing_campaign, state=missing_state,
    ))
    missing_alert = _existing_alert(db, missing_campaign)

    result = evaluate_daily_anomalies(db, TARGET, RuleConfig())

    assert complete_alert.status == "RECOVERED"
    assert missing_alert.status == "ONGOING"
    assert result["skipped_scope_details"][0]["campaign_id"] == missing_campaign
    assert result["notifications_queued"] == 1


@pytest.mark.parametrize("scope_complete", [False, True])
def test_empty_recovery_entity_ids_never_recover_anomalies(db, scope_complete):
    existing = _existing_alert(db)
    upsert_anomalies(
        db, STORE, [], evaluation_scope=f"{STORE}:campaign:sp",
        scope_complete=scope_complete, recovery_entity_ids=set(),
    )
    db.commit()

    assert existing.status == "ONGOING"
    assert existing.recovered_at is None
