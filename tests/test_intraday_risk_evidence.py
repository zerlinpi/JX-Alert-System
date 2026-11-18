"""Only current, same-source, fresh enabled SP budget observations can alert."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert.ai.intraday_risk_evidence import intraday_over_budget_candidates
from jx_alert.lingxing.intraday_observation import SOURCE
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    NotificationLog,
    RawSnapshot,
    Store,
    SyncJob,
)


CLOCK = datetime(2026, 11, 18, 19, tzinfo=timezone.utc)
CAPTURED = CLOCK.replace(tzinfo=None) - timedelta(minutes=1)
MASTER_TIME = CAPTURED - timedelta(minutes=1)
DAY = "2026-11-18"


@pytest.fixture
def warehouse():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(
            sid="5174",
            name="Geteen 美国",
            status=1,
            has_ads_setting=1,
            timezone="America/Los_Angeles",
            currency_code="USD",
        )
        campaign = Campaign(
            store_id="5174",
            campaign_type="sp",
            campaign_id="99",
            name="活动甲",
            state="enabled",
            budget=100,
            budget_type="daily",
            updated_at=MASTER_TIME,
            last_over_budget_at="2026-11-18T09:00:00-08:00",
        )
        master = RawSnapshot(
            source="lingxing_sp_campaigns",
            store_id="5174",
            captured_at=MASTER_TIME,
            payload={
                "rows": [
                    {
                        "campaign_id": "99",
                        "state": "enabled",
                        "daily_budget": 100,
                        "lastOverBudgetAt": campaign.last_over_budget_at,
                    }
                ]
            },
        )
        observation = RawSnapshot(
            source=SOURCE,
            store_id="5174",
            captured_at=CAPTURED,
            payload={
                "observation_only": True,
                "observation_date": DAY,
                "report_timezone": "America/Los_Angeles",
                "availability": "AVAILABLE",
                "sources": ["lingxing_openapi"],
                "rows": [
                    {
                        "campaign_id": "99",
                        "report_date": DAY,
                        "cost": 160,
                        "clicks": 45,
                        "impressions": 2000,
                        "orders": 0,
                        "confirmed_currency": "USD",
                    }
                ],
            },
        )
        db.add_all([store, campaign, master, observation])
        db.commit()
        yield db, store, campaign, master, observation
    engine.dispose()


def review(warehouse, **kwargs):
    db, store, _, _, snapshot = warehouse
    return intraday_over_budget_candidates(db, store, snapshot, now=CLOCK, **kwargs)


def test_real_budget_signal_has_proof_but_no_unmatured_orders_or_writes(warehouse):
    db, _, campaign, master, snapshot = warehouse
    payload = deepcopy(snapshot.payload)
    master_payload = deepcopy(master.payload)
    tables = (
        DailyMetric,
        DataCoverage,
        Campaign,
        RawSnapshot,
        SyncJob,
        AnomalyEvent,
        NotificationOutbox,
        NotificationLog,
    )
    before = [db.scalar(select(func.count()).select_from(table)) for table in tables]
    (row,) = review(warehouse)
    assert row["current"] == {"spend": 160, "clicks": 45, "impressions": 2000}
    assert row["verified_daily_budget"] == 100 and row["currency"] == "USD"
    assert row["intraday"] is True and row["material_signals"] == ["intraday_budget"]
    assert row["current_state"] == "投放中" and row["report_date"] == DAY
    assert row["mature"] == {} and row["baseline"] == {} and row["kpis"] == {}
    assert (
        row["proof"]["snapshot_id"] == snapshot.id
        and row["proof"]["master_snapshot_id"] == master.id
    )
    assert row["source_budget_event_date_basis"] == "explicit_offset"
    assert "orders" not in row["current"] and "orders" not in row["mature"]
    assert row == review(warehouse)[0]
    assert before == [db.scalar(select(func.count()).select_from(table)) for table in tables]
    assert not db.dirty and campaign.state == "enabled"
    assert snapshot.payload == payload and master.payload == master_payload


@pytest.mark.parametrize(
    "bad",
    [
        "unauthorized",
        "ads_unauthorized",
        "wrong_timezone",
        "wrong_store",
        "wrong_source",
        "old_day",
        "empty",
        "failed",
        "not_observation",
        "stale_observation",
        "future_observation",
        "stale_master",
        "master_not_this_round",
        "future_master",
        "failed_master",
        "paused",
        "raw_paused",
        "missing_master",
        "stale_state",
        "future_state",
        "wrong_budget",
        "lifetime_budget",
        "no_overbudget_event",
        "old_event",
        "future_event",
        "wrong_event",
        "missing_currency",
        "other_currency",
        "missing_spend",
        "missing_clicks",
        "missing_impressions",
        "fractional_clicks",
        "negative_spend",
        "nan_spend",
        "bool_spend",
        "duplicate_row",
        "duplicate_master",
        "low_relative_impact",
        "low_absolute_impact",
        "mcp_fallback",
        "unconfirmed_source",
    ],
)
def test_unverified_or_immaterial_cases_are_silent(warehouse, bad):
    db, store, campaign, master, snapshot = warehouse
    payload, masters = deepcopy(snapshot.payload), deepcopy(master.payload)
    row, identity = payload["rows"][0], masters["rows"][0]
    if bad == "unauthorized":
        store.status = 2
    elif bad == "ads_unauthorized":
        store.has_ads_setting = 0
    elif bad == "wrong_timezone":
        store.timezone = "not-an-IANA-zone"
    elif bad == "wrong_store":
        snapshot.store_id = "9999"
    elif bad == "wrong_source":
        snapshot.source = "mcp_campaign_sp"
    elif bad == "old_day":
        payload["observation_date"] = "2026-11-17"
    elif bad == "empty":
        payload["availability"] = "EMPTY"
    elif bad == "failed":
        payload["availability"] = "FAILED"
    elif bad == "not_observation":
        payload["observation_only"] = False
    elif bad == "stale_observation":
        snapshot.captured_at = CAPTURED - timedelta(hours=2)
    elif bad == "future_observation":
        snapshot.captured_at = CLOCK.replace(tzinfo=None) + timedelta(seconds=1)
    elif bad == "stale_master":
        master.captured_at = MASTER_TIME - timedelta(hours=2)
    elif bad == "master_not_this_round":
        master.captured_at = CAPTURED - timedelta(minutes=16)
    elif bad == "future_master":
        master.captured_at = CLOCK.replace(tzinfo=None) + timedelta(seconds=1)
    elif bad == "failed_master":
        masters["status"] = "FAILED"
    elif bad == "paused":
        campaign.state = "paused"
    elif bad == "raw_paused":
        identity["state"] = "paused"
    elif bad == "missing_master":
        masters["rows"] = []
    elif bad == "stale_state":
        campaign.updated_at = MASTER_TIME - timedelta(hours=2)
    elif bad == "future_state":
        campaign.updated_at = CLOCK.replace(tzinfo=None) + timedelta(seconds=1)
    elif bad == "wrong_budget":
        campaign.budget = 99
    elif bad == "lifetime_budget":
        campaign.budget_type = identity["budget_type"] = "lifetime"
    elif bad == "no_overbudget_event":
        campaign.last_over_budget_at = identity["lastOverBudgetAt"] = None
    elif bad == "old_event":
        campaign.last_over_budget_at = identity["lastOverBudgetAt"] = "2026-11-17T09:00:00-08:00"
    elif bad == "future_event":
        campaign.last_over_budget_at = identity["lastOverBudgetAt"] = "2026-11-18T23:00:00-08:00"
    elif bad == "wrong_event":
        campaign.last_over_budget_at = "2026-11-18T09:01:00-08:00"
    elif bad == "missing_currency":
        row.pop("confirmed_currency")
    elif bad == "other_currency":
        row["confirmed_currency"] = "CAD"
    elif bad == "missing_spend":
        row.pop("cost")
    elif bad == "missing_clicks":
        row.pop("clicks")
    elif bad == "missing_impressions":
        row.pop("impressions")
    elif bad == "fractional_clicks":
        row["clicks"] = 4.5
    elif bad == "negative_spend":
        row["cost"] = -1
    elif bad == "nan_spend":
        row["cost"] = float("nan")
    elif bad == "bool_spend":
        row["cost"] = True
    elif bad == "duplicate_row":
        payload["rows"].append(dict(row))
    elif bad == "duplicate_master":
        masters["rows"].append(dict(identity))
    elif bad == "low_relative_impact":
        campaign.budget = identity["daily_budget"] = 140
    elif bad == "low_absolute_impact":
        campaign.budget = identity["daily_budget"] = 25
        row["cost"] = 40
    elif bad == "mcp_fallback":
        payload["sources"] = ["official_mcp_observation"]
    elif bad == "unconfirmed_source":
        row["_attribution_unconfirmed"] = True
    snapshot.payload, master.payload = payload, masters
    db.commit()
    assert review(warehouse) == []
    assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_explicit_offset_event_uses_store_day_instead_of_utc_date(warehouse):
    db, _, campaign, master, _ = warehouse
    payload = deepcopy(master.payload)
    event = "2026-11-19T01:00:00+08:00"  # November 18 at 09:00 Pacific.
    payload["rows"][0]["lastOverBudgetAt"] = campaign.last_over_budget_at = event
    master.payload = payload
    db.commit()
    assert review(warehouse)[0]["source_budget_event"] == event


def test_naive_source_time_is_kept_literal_without_timezone_conversion(warehouse):
    db, _, campaign, master, _ = warehouse
    payload = deepcopy(master.payload)
    event = "2026-11-18 09:00:00"
    payload["rows"][0]["lastOverBudgetAt"] = campaign.last_over_budget_at = event
    master.payload = payload
    db.commit()
    (result,) = review(warehouse)
    assert (
        result["source_budget_event"] == event
        and result["source_budget_event_date_basis"] == "raw_date"
    )


def test_newer_failed_observation_or_master_does_not_revive_old_success(warehouse):
    db, _, _, _, snapshot = warehouse
    db.add(
        RawSnapshot(
            source=SOURCE,
            store_id="5174",
            captured_at=CAPTURED,
            payload={"observation_date": DAY, "availability": "FAILED"},
        )
    )
    db.commit()
    assert review(warehouse) == []
    assert snapshot.payload["availability"] == "AVAILABLE"


def test_current_event_and_exact_boundary_is_a_risk_not_zero_conversion(warehouse):
    db, _, campaign, master, snapshot = warehouse
    payload, identity = deepcopy(snapshot.payload), deepcopy(master.payload)
    campaign.budget = identity["rows"][0]["daily_budget"] = 150
    payload["rows"][0].update(cost=180, orders=999)
    snapshot.payload, master.payload = payload, identity
    db.commit()
    (result,) = review(warehouse)
    assert result["current"]["spend"] == 180 and result["verified_daily_budget"] == 150
    assert result["material_signals"] == ["intraday_budget"] and result["mature"] == {}


@pytest.mark.parametrize("clock,age", [(CLOCK.replace(tzinfo=None), 75), (CLOCK, 0), (CLOCK, True)])
def test_invalid_internal_clock_or_freshness_fails_closed(warehouse, clock, age):
    db, store, _, _, snapshot = warehouse
    with pytest.raises(ValueError):
        intraday_over_budget_candidates(db, store, snapshot, now=clock, max_age_minutes=age)
