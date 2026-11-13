from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.chat_analysis import prepare_review, submit_review
from jx_alert.config import Settings
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store, SyncJob


CLOCK = datetime(2026, 11, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path):
    scope, zone, routing = (tmp_path / name for name in ["scope.json", "zone.json", "routing.json"])
    scope.write_text(json.dumps({"store_ids": [5174]}))
    zone.write_text(json.dumps({"stores": {"5174": {"timezone": "America/Los_Angeles", "currency_code": "USD"}}}))
    routing.write_text(json.dumps({"default_chat_id": "test", "groups": {"test": {"chat_id": "oc_test"}},
                                   "stores": {"5174": {"open_ids": ["ou_operator"]}}}))
    settings = Settings(_env_file=None, collection_scope_file=str(scope), store_timezones_file=str(zone),
                        feishu_routing_file=str(routing), feishu_daily_report_chat_id="test", feishu_transport="app")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid="5174", name="测试店铺", status=1, timezone="America/Los_Angeles", currency_code="USD"))
        for day, value in [(date(2026, 11, 10), 10), (date(2026, 11, 11), 0)]:
            db.add(DailyMetric(store_id="5174", dimension="store", entity_id="5174", metric_date=day,
                               metric_name="orders", value=value))
            db.add(DataCoverage(store_id="5174", dimension="store", metric_date=day, status="COMPLETE"))
        db.flush()
        yield db, settings
    engine.dispose()


def finding(review):
    return [{"topic": "订单异常", "summary": "订单从十单下降至零单，建议核查销售情况",
             "action": "核查库存及订单同步", "evidence_ids": [r["id"] for r in review["facts"]]}]


def test_no_anomaly_is_audited_without_any_notification(setup):
    db, s = setup
    review = prepare_review(db, s, "5174", now=CLOCK)
    result = submit_review(db, s, review["review_id"], [], now=CLOCK)
    assert result["status"] == "NO_ANOMALY" and result["outbox_ids"] == []
    assert db.scalars(select(NotificationOutbox)).all() == []
    assert db.get(SyncJob, review["review_id"]).status == "REVIEWED"


def test_same_incident_deduplicates_across_hourly_reviews_and_keeps_chinese_tables(setup):
    db, s = setup
    first = prepare_review(db, s, "5174", now=CLOCK)
    result = submit_review(db, s, first["review_id"], finding(first), now=CLOCK)
    assert result["status"] == "QUEUED"
    assert submit_review(db, s, first["review_id"], finding(first), now=CLOCK) == result
    second = prepare_review(db, s, "5174", now=CLOCK + timedelta(hours=1))
    updated = finding(second)
    updated[0]["summary"] = "订单已下降，需核实销售情况"
    assert submit_review(db, s, second["review_id"], updated, now=CLOCK + timedelta(hours=1))["outbox_ids"] == result["outbox_ids"]
    jobs = db.scalars(select(NotificationOutbox)).all()
    assert len(jobs) == 1 and jobs[0].payload["delivery"]["open_ids"] == ["ou_operator"]
    assert any(e["tag"] == "table" for e in jobs[0].payload["card"]["body"]["elements"])


@pytest.mark.parametrize("problem", ["incomplete", "revised", "invented_id", "english", "mention", "expired", "different_entity", "routing"])
def test_invalid_evidence_cannot_notify(setup, problem):
    db, s = setup
    review = prepare_review(db, s, "5174", now=CLOCK)
    proposed = finding(review)
    clock = CLOCK
    if problem == "incomplete":
        db.scalar(select(DataCoverage)).status = "FAILED"
    elif problem == "revised":
        db.scalar(select(DailyMetric)).value = 777
    elif problem == "invented_id":
        proposed[0]["evidence_ids"] = [123456, 654321]
    elif problem == "english":
        proposed[0]["summary"] = "Sales orders decreased"
    elif problem == "mention":
        proposed[0]["summary"] = "请<at id=all>核查"
    elif problem == "expired":
        clock += timedelta(hours=3)
    elif problem == "different_entity":
        db.scalar(select(DailyMetric)).entity_id = "another"
    else:
        path = s.feishu_routing_file
        route = json.loads(open(path).read())
        route["stores"]["5174"]["open_ids"] = []
        open(path, "w").write(json.dumps(route))
    db.flush()
    with pytest.raises(ValueError):
        submit_review(db, s, review["review_id"], proposed, now=clock)
    assert db.scalars(select(NotificationOutbox)).all() == []


def test_new_store_or_timezone_mismatch_cannot_produce_review(setup):
    db, s = setup
    with pytest.raises(ValueError, match="scope"):
        prepare_review(db, s, "999", now=CLOCK)
    db.get(Store, "5174").timezone = "UTC"
    db.flush()
    with pytest.raises(ValueError, match="timezone"):
        prepare_review(db, s, "5174", now=CLOCK)


def test_recent_advertising_orders_cannot_be_presented_as_final_conversion_anomaly(setup):
    db, s = setup
    for r in db.scalars(select(DailyMetric)):
        r.dimension = "campaign_sp"
    for r in db.scalars(select(DataCoverage)):
        r.dimension = "campaign_sp"
    db.flush()
    review = prepare_review(db, s, "5174", dimension="campaign_sp", now=CLOCK)
    with pytest.raises(ValueError, match="mature"):
        submit_review(db, s, review["review_id"], finding(review), now=CLOCK)
