"""Maturity and partition evidence gates precede any model interpretation."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert.ai.ad_review_evidence import build_ad_review_context
from jx_alert.config import Settings
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AnomalyEvent, Campaign, DailyMetric, DataCoverage, NotificationLog, RawSnapshot, Store, SyncJob,
)


DAY = date(2026, 11, 16)
CLOCK = datetime(2026, 11, 18, 1, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 11, 18)


@pytest.fixture
def warehouse():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(sid="5174", name="Geteen 美国", status=1, timezone="America/Los_Angeles")
        db.add(store)
        db.commit()
        yield db, store, Settings(_env_file=None)
    engine.dispose()


def seed(db, sid="5174", kind="sp", campaign_id="99", *, days=None):
    lag = 7 if kind == "sp" else 14
    days = days or [DAY - timedelta(days=i) for i in range(lag + 9)]
    campaign = Campaign(store_id=sid, campaign_id=campaign_id, campaign_type=kind,
        name="测试活动", state="enabled", updated_at=CAPTURED)
    db.add(campaign)
    for day in days:
        db.add(DataCoverage(store_id=sid, metric_date=day, dimension="campaign_" + kind,
            status="COMPLETE", row_count=1, completed_at=CAPTURED))
        db.add(RawSnapshot(store_id=sid, source="lingxing_ads_" + kind + "_campaign_report", captured_at=CAPTURED,
            payload={"report_type": kind, "report_date": day.isoformat(), "rows": [{"campaign_id": campaign_id,
                "cost": 50, "orders": 5, "sales": 200, "units": 5, "clicks": 50, "impressions": 10000}]}))
        for metric, value in {"spend": 50, "orders": 5, "sales": 200, "units": 5,
                              "clicks": 50, "impressions": 10000}.items():
            db.add(DailyMetric(store_id=sid, metric_date=day, dimension="campaign_" + kind,
                entity_id=campaign_id, metric_name=metric, value=value,
                currency="USD" if metric in {"spend", "sales"} else None,
                source="lingxing_ads_" + kind))
    db.commit()
    return campaign


def review(warehouse, **kwargs):
    db, store, settings = warehouse
    return build_ad_review_context(db, settings, [store], {store.sid: DAY}, now=CLOCK, **kwargs)


def fact(db, metric, on=None, *, kind="sp"):
    return db.scalar(select(DailyMetric).where(DailyMetric.metric_name == metric,
        DailyMetric.metric_date == (on or DAY - timedelta(days=7)), DailyMetric.dimension == "campaign_" + kind))


def coverage(db, on=None, *, kind="sp"):
    return db.scalar(select(DataCoverage).where(DataCoverage.metric_date == (on or DAY - timedelta(days=7)),
        DataCoverage.dimension == "campaign_" + kind))


def snapshot(db, on=None):
    return db.scalar(select(RawSnapshot).where(RawSnapshot.payload["report_date"].as_string()
        == (on or DAY - timedelta(days=7)).isoformat()))


def update_raw(db, metric, value, on=None):
    from copy import deepcopy
    record = snapshot(db, on)
    payload = deepcopy(record.payload)
    payload["rows"][0]["cost" if metric == "spend" else metric] = value
    record.payload = payload


def test_complete_mature_source_keeps_exact_facts_and_leaves_all_tables_unchanged(warehouse):
    db, store, settings = warehouse
    seed(db)
    fact(db, "orders").value = 0
    fact(db, "sales").value = 0
    fact(db, "units").value = 0
    for metric in ("orders", "sales", "units"):
        update_raw(db, metric, 0)
    db.commit()
    tables = (DailyMetric, DataCoverage, Campaign, SyncJob, AnomalyEvent, NotificationOutbox, NotificationLog)
    before = [db.scalar(select(func.count()).select_from(table)) for table in tables]
    context = review(warehouse)
    row, = context["campaigns"]
    assert row["mature_date"] == "2026-11-09" and row["report_date"] == "2026-11-16"
    assert row["mature"]["orders"] == 0 and row["mature"]["sales"] == 0
    assert row["mature"]["spend"] == 50 and row["currency"] == "USD"
    assert "orders" not in row["current"] and "sales" not in row["current"]
    assert row["baseline"]["basis"] == "SEVEN_DAY" and row["baseline"]["orders"] == 5
    assert "zero_conversion" in row["material_signals"]
    assert row["source"] == "lingxing_ads_sp" and row["current_state"] == "投放中"
    assert context["policy"]["advertising_changes_allowed"] is False
    assert before == [db.scalar(select(func.count()).select_from(table)) for table in tables]
    assert context == review(warehouse)


@pytest.mark.parametrize("failure", ["missing_metric", "missing_current", "failed_partition", "truncated",
                                     "old_observation", "future_capture", "unknown_currency", "mixed_currency", "mixed_source",
                                     "missing_raw", "old_raw_new_coverage", "revised_raw", "raw_future", "failed_raw"])
def test_incomplete_or_not_reobserved_mature_campaign_never_enters_model_context(warehouse, failure):
    db, _, _ = warehouse
    seed(db)
    if failure == "missing_metric":
        db.delete(fact(db, "orders"))
    elif failure == "missing_current":
        db.delete(fact(db, "clicks", DAY))
    elif failure == "failed_partition":
        coverage(db).status = "FAILED"
    elif failure == "truncated":
        coverage(db).row_count = 2
    elif failure == "old_observation":
        # Calendar age alone does not mature facts fetched on the next morning.
        coverage(db).completed_at = datetime(2026, 11, 10, 9)
    elif failure == "future_capture":
        coverage(db).completed_at = CLOCK.replace(tzinfo=None) + timedelta(seconds=1)
    elif failure == "unknown_currency":
        fact(db, "spend").currency = None
    elif failure == "mixed_currency":
        fact(db, "sales").currency = "CAD"
    elif failure == "mixed_source":
        fact(db, "orders").source = "lingxing_mcp_campaign_report"
    elif failure == "missing_raw":
        db.delete(snapshot(db))
    elif failure == "old_raw_new_coverage":
        snapshot(db).captured_at = datetime(2026, 11, 10)
    elif failure == "revised_raw":
        update_raw(db, "spend", 999)
    elif failure == "raw_future":
        snapshot(db).captured_at = CLOCK.replace(tzinfo=None) + timedelta(seconds=1)
    elif failure == "failed_raw":
        record = snapshot(db)
        record.payload = {**record.payload, "status": "FAILED"}
    db.commit()
    context = review(warehouse)
    assert context["campaigns"] == []
    assert "missing" not in context and "gaps" not in context


def test_seven_day_baseline_falls_back_only_to_exact_mature_previous_day(warehouse):
    db, _, _ = warehouse
    seed(db)
    coverage(db, DAY - timedelta(days=10)).status = "FAILED"
    db.commit()
    row, = review(warehouse)["campaigns"]
    assert row["baseline"]["basis"] == "PREVIOUS_DAY"
    assert row["baseline"]["dates"] == ["2026-11-08"]
    coverage(db, DAY - timedelta(days=8)).completed_at = datetime(2026, 11, 10)
    db.commit()
    assert review(warehouse)["campaigns"] == []


def test_mcp_cost_observations_cannot_borrow_primary_attribution(warehouse):
    db, _, _ = warehouse
    seed(db)
    for row in db.scalars(select(DailyMetric).where(DailyMetric.metric_name.in_(["spend", "clicks", "impressions"]))):
        row.dimension = "mcp_campaign_sp"
        row.source = "lingxing_mcp_campaign_report"
    db.commit()
    assert review(warehouse)["campaigns"] == []


def test_unrelated_failed_daily_job_does_not_hide_complete_ad_partition(warehouse):
    db, _, _ = warehouse
    seed(db)
    db.add(SyncJob(job_type="daily_collection", status="PARTIAL_FAILED", started_at=CAPTURED,
        metadata_json={"report_dates": {"5174": DAY.isoformat()}, "results": {"sales": {"status": "PARTIAL_FAILED"}}}))
    db.commit()
    assert len(review(warehouse)["campaigns"]) == 1


def test_normal_or_improved_campaigns_have_no_material_adverse_signal(warehouse):
    db, _, _ = warehouse
    seed(db)
    fact(db, "orders").value = 10
    fact(db, "sales").value = 400
    fact(db, "units").value = 10
    for metric, value in (("orders", 10), ("sales", 400), ("units", 10)):
        update_raw(db, metric, value)
    db.commit()
    row, = review(warehouse)["campaigns"]
    assert row["material_signals"] == []
    assert row["kpis"]["ROAS"]["status"] == "NON_ADVERSE"
    assert row["kpis"]["CPA"]["status"] == "NON_ADVERSE"


def test_paused_campaign_is_labelled_as_recorded_not_current_budget_waste(warehouse):
    db, _, _ = warehouse
    campaign = seed(db)
    campaign.state = "paused"
    db.commit()
    row, = review(warehouse)["campaigns"]
    assert row["current_state"] == "已暂停"
    assert "budget" not in row


def test_sb_sd_require_explicit_selection_and_their_own_fourteen_day_deadline(warehouse):
    db, _, _ = warehouse
    seed(db, kind="sb")
    assert review(warehouse)["campaigns"] == []
    row, = review(warehouse, ad_types=("sb",))["campaigns"]
    assert row["mature_date"] == "2026-11-02" and row["attribution_days"] == 14
    coverage(db, date(2026, 11, 2), kind="sb").completed_at = datetime(2026, 11, 10)
    db.commit()
    assert review(warehouse, ad_types=("sb",))["campaigns"] == []


def test_authorization_unknown_timezone_and_unfinished_day_are_not_reviewed(warehouse):
    db, store, settings = warehouse
    seed(db)
    store.status = 2
    assert review(warehouse)["campaigns"] == []
    store.status = 1
    store.timezone = None
    assert review(warehouse)["campaigns"] == []
    store.timezone = "America/Los_Angeles"
    assert build_ad_review_context(db, settings, [store], {store.sid: "2026-11-17"}, now=CLOCK)["campaigns"] == []


def test_observation_revision_changes_evidence_fingerprint(warehouse):
    db, _, _ = warehouse
    seed(db)
    before = review(warehouse)["campaigns"][0]["evidence_id"]
    fact(db, "spend").value = 51
    update_raw(db, "spend", 51)
    db.commit()
    assert review(warehouse)["campaigns"][0]["evidence_id"] != before


def test_future_campaign_master_never_claims_current_enabled_state(warehouse):
    db, _, _ = warehouse
    campaign = seed(db)
    campaign.updated_at = CLOCK.replace(tzinfo=None) + timedelta(seconds=1)
    db.commit()
    row, = review(warehouse)["campaigns"]
    assert row["current_state"] == "状态未确认" and row["state_observed_at"] is None
