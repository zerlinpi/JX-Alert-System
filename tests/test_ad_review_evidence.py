"""Maturity and partition evidence gates precede any model interpretation."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from jx_alert.ai.ad_review_evidence import automatic_sp_signals, build_ad_review_context
from jx_alert.ai.ad_review_skills import load_ad_review_skills
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


def _revise(db, day, **changes):
    for metric, value in changes.items():
        fact(db, metric, day).value = value
        update_raw(db, metric, value, day)


def test_spend_increase_and_order_decline_with_same_efficiency_do_not_trigger(warehouse):
    db, _, _ = warehouse
    seed(db)
    for prior in range(8, 15):
        _revise(db, DAY - timedelta(days=prior), orders=10, clicks=100, spend=100, units=10, sales=400)
    # Mature period halves volume and spend, leaving efficiency unchanged.
    # The current report doubles spend and clicks with unchanged CPC and improved CTR.
    _revise(db, DAY, spend=100, clicks=100)
    db.commit()
    row, = review(warehouse)["campaigns"]
    assert row["baseline"]["orders"] == 10 and row["mature"]["orders"] == 5
    assert row["material_signals"] == []


def test_conflicting_zero_and_low_expected_conversion_do_not_trigger(warehouse):
    db, _, _ = warehouse
    seed(db)
    mature = DAY - timedelta(days=7)
    _revise(db, mature, orders=0)  # Sales and units still report attribution.
    db.commit()
    assert review(warehouse)["campaigns"][0]["material_signals"] == []
    for prior in range(8, 15):
        _revise(db, DAY - timedelta(days=prior), orders=1)
    _revise(db, mature, sales=0, units=0)
    db.commit()
    row, = review(warehouse)["campaigns"]
    assert row["mature_comparison"]["expected_orders_at_reference_rate"] == 1
    assert row["material_signals"] == []


def test_active_profile_hot_reloads_and_only_tightens_gate(warehouse, tmp_path):
    db, _, settings = warehouse
    seed(db)
    _revise(db, DAY, clicks=5)
    db.commit()
    path = tmp_path / "ad-review-skills.json"
    settings.ad_review_skills_file = str(path)
    before, = review(warehouse)["campaigns"]
    assert automatic_sp_signals(before) == ["CTR"]
    path.write_text(json.dumps({"version": 1, "defaults": {"min_impressions": 11000},
        "ad_types": {"sp": {"objective": "discovery"}},
        "campaigns": {"5174:sp:99": {"objective": "efficiency", "min_impressions": 4000}}}))
    after, = review(warehouse)["campaigns"]
    assert after["ad_skill"]["min_impressions"] == 11000
    assert after["ad_skill"]["objective"] == "efficiency"
    assert after["evidence_id"] != before["evidence_id"] and not automatic_sp_signals(after)
    path.write_text('{"version":1,"defaults":{"prompt":"任意指令"}}', encoding="utf-8")
    invalid, = review(warehouse)["campaigns"]
    assert invalid["ad_skill"]["status"] == "invalid"
    assert invalid["material_signals"] == ["CTR"]  # Explicit read-only review still has the proof.
    assert automatic_sp_signals(invalid) == []


@pytest.mark.parametrize(("impressions", "clicks", "reference", "included"), [
    (2000, 0, .004, False),
    (2000, 0, .005, True),
    (4046, 28, .0121833693, True),
    (None, 0, .013, False),
])
def test_proactive_ctr_uses_verified_reference_click_gap(impressions, clicks, reference, included):
    row = {"report_date": DAY.isoformat(), "ad_type": "sp", "material_signals": ["CTR"],
           "current": {"impressions": impressions, "clicks": clicks},
           "kpis": {"CTR": {"baseline": reference}}}
    assert automatic_sp_signals(row) == (["CTR"] if included else [])


@pytest.mark.parametrize("document", [
    '{"version":1,"version":1}', '{"version":true}', '{"version":1,"defaults":{"min_clicks":4}}',
    '{"version":1,"ad_types":{"sponsored":{"min_clicks":40}}}',
    '{"version":1,"campaigns":{"*:sp:99":{}}}', '{"version":1,"defaults":{"min_relative_change":NaN}}',
])
def test_skills_reject_invalid_or_unsafe_threshold_files(document, tmp_path):
    path = tmp_path / "skills.json"
    path.write_text(document)
    settings = Settings(_env_file=None, ad_review_skills_file=str(path))
    assert load_ad_review_skills(settings).status == "invalid"
