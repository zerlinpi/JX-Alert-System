from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.analysis.ad_kpi_review import prepare_kpi_review, submit_kpi_review
from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.config import Settings
from jx_alert.notifications.daily_report import build_daily_card
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, Store


CLOCK = datetime(2026, 11, 13, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 11)


@pytest.fixture
def warehouse(tmp_path):
    scope, zones, routing = (tmp_path / name for name in ("scope.json", "zones.json", "route.json"))
    scope.write_text(json.dumps({"store_ids": [5174]}))
    zones.write_text(json.dumps({"stores": {"5174": {"timezone": "America/Los_Angeles"}}}))
    routing.write_text(json.dumps({"default_chat_id": "group", "groups": {
        "group": {"chat_id": "oc_handover", "admin_open_ids": ["ou_admin"]}},
        "stores": {"5174": {"open_ids": ["ou_operator"]}}}))
    settings = Settings(_env_file=None, collection_scope_file=str(scope), store_timezones_file=str(zones),
        feishu_routing_file=str(routing), feishu_daily_report_chat_id="group", feishu_transport="app")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid="5174", name="测试", status=1, timezone="America/Los_Angeles", currency_code="USD"))
        for kind in ("sp", "sb", "sd"):
            db.add(Campaign(store_id="5174", campaign_type=kind, campaign_id=kind, name="活动", state="enabled"))
        for offset in range(22):
            day = DAY - timedelta(days=offset)
            db.add(DataCoverage(store_id="5174", metric_date=day, dimension="store", status="COMPLETE"))
            db.add(DailyMetric(store_id="5174", metric_date=day, dimension="store", entity_id="5174",
                metric_name="sales", value=1500 if not offset else 3000, currency="USD"))
            for kind in ("sp", "sb", "sd"):
                dimension = "campaign_" + kind
                db.add(DataCoverage(store_id="5174", metric_date=day, dimension=dimension, status="COMPLETE"))
                for metric, value in {"spend": 100, "sales": 1000, "orders": 10, "units": 10,
                                      "clicks": 100, "impressions": 10000}.items():
                    if metric == "clicks" and offset == 0:
                        value = 50
                    db.add(DailyMetric(store_id="5174", metric_date=day, dimension=dimension, entity_id=kind,
                        metric_name=metric, value=value, currency="USD" if metric in {"sales", "spend"} else None))
        db.flush()
        yield db, settings
    engine.dispose()


def test_review_targets_real_issue_includes_all_kpis_and_deduplicates(warehouse):
    db, settings = warehouse
    review = prepare_kpi_review(db, settings, "5174", now=CLOCK)
    assert review["kpis"]["TCOAS"]["current"] == 0.2
    findings = [{"metric": "CPC", "summary": "点击花费上升", "action": "核查关键词与广告位"}]
    result = submit_kpi_review(db, settings, review["review_id"], findings, now=CLOCK)
    assert result["notification_titles"] == ["点击花费上升"]
    row = db.get(NotificationOutbox, result["outbox_ids"][0])
    tables = [e for e in row.payload["card"]["body"]["elements"] if e["tag"] == "table"]
    assert len(tables[-1]["rows"]) == 7
    assert row.payload["delivery"]["receive_id"] == "oc_handover"
    assert set(row.payload["delivery"]["open_ids"]) == {"ou_admin", "ou_operator"}
    assert submit_kpi_review(db, settings, review["review_id"], findings, now=CLOCK) == result
    newer = prepare_kpi_review(db, settings, "5174", now=CLOCK + timedelta(minutes=30))
    assert submit_kpi_review(db, settings, newer["review_id"], findings, now=CLOCK + timedelta(minutes=30))["outbox_ids"] == result["outbox_ids"]


@pytest.mark.parametrize("change", ["value", "coverage", "currency", "threshold", "expired"])
def test_revised_or_expired_kpi_review_cannot_send(warehouse, change):
    db, settings = warehouse
    review = prepare_kpi_review(db, settings, "5174", dimension="campaign_sp", entity_id="sp", now=CLOCK)
    clock = CLOCK
    if change == "threshold":
        settings.alert_kpi_change_ratio = 0.2
    elif change == "expired":
        clock += timedelta(hours=3)
    elif change == "coverage":
        db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY, DataCoverage.dimension == "campaign_sp")).status = "FAILED"
    else:
        row = db.scalar(select(DailyMetric).where(DailyMetric.metric_date == DAY,
            DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend"))
        if change == "value":
            row.value += 1
        else:
            row.currency = "CAD"
    db.flush()
    with pytest.raises(ValueError):
        submit_kpi_review(db, settings, review["review_id"], [{"metric": "CPC", "summary": "点击花费上升",
            "action": "核查投放"}], now=clock)
    assert db.scalar(select(NotificationOutbox)) is None


def test_missing_total_ad_type_protects_store_ratios_and_daily_card_keeps_seven_rows(warehouse):
    db, settings = warehouse
    db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY,
        DataCoverage.dimension == "campaign_sd")).status = "FAILED"
    db.flush()
    comparisons = advertising_kpis(db, settings, "5174", DAY)
    assert comparisons["TCOAS"]["current"] is None
    value = build_daily_card(db, db.get(Store, "5174"), DAY, settings=settings)
    tables = [e for e in value["body"]["elements"] if e["tag"] == "table"]
    assert len(tables[-1]["rows"]) == 7


@pytest.mark.parametrize("field,value", [("state", "paused"), ("name", "更名后的活动")])
def test_changed_campaign_identity_or_state_requires_fresh_review(warehouse, field, value):
    db, settings = warehouse
    review = prepare_kpi_review(db, settings, "5174", dimension="campaign_sp", entity_id="sp", now=CLOCK)
    campaign = db.scalar(select(Campaign).where(Campaign.store_id == "5174",
        Campaign.campaign_type == "sp", Campaign.campaign_id == "sp"))
    setattr(campaign, field, value)
    db.flush()
    with pytest.raises(ValueError, match="object name or state changed"):
        submit_kpi_review(db, settings, review["review_id"], [{"metric": "CPC",
            "summary": "点击花费上升", "action": "核查投放"}], now=CLOCK)
    assert db.scalar(select(NotificationOutbox)) is None


def test_nonadverse_kpi_stays_silent_and_campaign_tcoas_stays_unavailable(warehouse):
    db, settings = warehouse
    review = prepare_kpi_review(db, settings, "5174", dimension="campaign_sp", entity_id="sp", now=CLOCK)
    assert review["kpis"]["TCOAS"]["status"] == "SCOPE_UNSUPPORTED"
    result = submit_kpi_review(db, settings, review["review_id"], [{"metric": "ROAS",
        "summary": "广告投入产出下降", "action": "复核投放"}], now=CLOCK)
    assert result["outbox_ids"] == []


def test_missing_entire_campaign_does_not_silently_lower_store_total(warehouse):
    db, settings = warehouse
    record = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY,
        DataCoverage.dimension == "campaign_sp"))
    record.row_count = 2  # Only one source campaign is actually present.
    db.flush()
    comparisons = advertising_kpis(db, settings, "5174", DAY)
    assert comparisons["CTR"]["status"] == "INSUFFICIENT_DATA"
    assert comparisons["TCOAS"]["current"] is None
