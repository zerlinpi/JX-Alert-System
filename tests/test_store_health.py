"""Health briefs expose observations without manufacturing current health."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from jx_alert.analysis.store_health import INDICATORS, store_health_snapshot
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AnomalyEvent, PerformanceNotice, PerformanceSyncState, Store, StorePerformance,
)


NOW = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
VALUES = {
    "order_with_defect": 0.5, "late_shipment": 0.2, "pre_fulfillment_cancellation": 0.2,
    "valid_tracking": 99, "on_time_delivery": 99, "fba_order_with_defect": 0.5,
    "invoice_defect": 0.2,
}
STANDARDS = {
    "order_with_defect": 1, "late_shipment": 4, "pre_fulfillment_cancellation": 2,
    "valid_tracking": 95, "on_time_delivery": 97, "fba_order_with_defect": 1,
    "invoice_defect": 5,
}


@pytest.fixture
def warehouse():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(sid="101", name="测试店铺", status=1)
        performance = StorePerformance(
            store_id="101", pull_date=date(2026, 11, 12),
            source_updated_at=(NOW - timedelta(minutes=10)).isoformat(),
            captured_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None),
            ahr_score=245, ahr_status="Healthy", freshness_status="FRESH",
            indicators=dict(VALUES), standards=dict(STANDARDS), breaches=[],
        )
        settings = SimpleNamespace(lingxing_performance_source_timezone="UTC",
            lingxing_performance_max_age_hours=8, lingxing_performance_observation_only=True)
        db.add_all([store, performance])
        db.flush()
        yield db, store, settings, performance
    engine.dispose()


def snapshot(warehouse, **kwargs):
    db, store, settings, _ = warehouse
    return store_health_snapshot(db, store, settings, now=kwargs.get("now", NOW))


def test_complete_fresh_known_health_is_healthy_and_capture_is_explicit_utc(warehouse):
    result = snapshot(warehouse)
    assert result["status"] == "HEALTHY"
    assert result["status_label"] == "健康"
    assert result["source_status_label"] == "健康"
    assert result["ahr_score"] == 245
    assert result["pull_date"] == "2026-11-12"
    assert result["captured_at"] == "2026-11-12T23:55:00+00:00"
    assert result["freshness_status"] == "FRESH"
    assert {r["field"] for r in result["indicators"]} == set(INDICATORS)
    assert all(r["status"] == "WITHIN_STANDARD" for r in result["indicators"])
    assert result["issues"] == []


def test_read_time_freshness_does_not_reuse_the_collectors_old_fresh_flag(warehouse):
    result = snapshot(warehouse, now=NOW + timedelta(hours=9))
    assert result["status"] == "UNCONFIRMED"
    assert result["freshness_status"] == "STALE"
    assert result["source_status_label"] == "健康"


@pytest.mark.parametrize("source,status", [(None, "MISSING"), ("unparseable", "MISSING"),
    ((NOW + timedelta(minutes=10)).isoformat(), "FUTURE")])
def test_missing_or_future_source_time_never_becomes_healthy(warehouse, source, status):
    db, _, _, row = warehouse
    row.source_updated_at = source
    db.flush()
    result = snapshot(warehouse)
    assert result["freshness_status"] == status
    assert result["status"] == "UNCONFIRMED"


def test_unconfirmed_source_timezone_preserves_risk_as_observation_only(warehouse):
    db, _, settings, row = warehouse
    settings.lingxing_performance_source_timezone = ""
    row.ahr_status = "AtRisk"
    row.indicators = {**VALUES, "order_with_defect": 2.5}
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "UNCONFIRMED"
    assert result["source_status_label"] == "有风险"
    assert result["freshness_status"] == "NOT_CONFIGURED"
    assert result["issues"] == ["订单缺陷率2.5%高于平台标准1%"]
    assert db.scalar(select(func.count(AnomalyEvent.id))) == 0


@pytest.mark.parametrize("state", ["FAILED", "MISSING"])
def test_newer_failed_or_empty_sync_masks_an_older_healthy_snapshot(warehouse, state):
    db, _, _, _ = warehouse
    db.add(PerformanceSyncState(store_id="101", dataset="store_performance", status=state,
        captured_at=NOW.replace(tzinfo=None), record_count=0))
    db.flush()
    result = snapshot(warehouse)
    assert result["source_status_label"] == "健康"
    assert result["freshness_status"] == state
    assert result["status"] == "UNCONFIRMED"


def test_failed_sync_older_than_a_new_observation_does_not_replace_its_source(warehouse):
    db, _, _, _ = warehouse
    db.add(PerformanceSyncState(store_id="101", dataset="store_performance", status="FAILED",
        captured_at=(NOW - timedelta(days=1)).replace(tzinfo=None), record_count=0))
    db.flush()
    assert snapshot(warehouse)["status"] == "HEALTHY"


@pytest.mark.parametrize("store_status,expected", [(2, "AUTHORIZATION_FAILED"), (0, "INACTIVE"),
    (3, "INACTIVE"), (99, "UNCONFIRMED")])
def test_store_authorization_and_activation_take_priority(warehouse, store_status, expected):
    db, store, _, _ = warehouse
    store.status = store_status
    db.flush()
    assert snapshot(warehouse)["status"] == expected


def test_no_snapshot_is_no_data_and_all_seven_missing_fields_stay_visible(warehouse):
    db, _, _, row = warehouse
    db.delete(row)
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "NO_DATA"
    assert result["ahr_score"] is None and result["captured_at"] is None
    assert result["source_status_label"] == "未提供"
    assert len(result["indicators"]) == 7
    assert all(r["status"] == "INSUFFICIENT_DATA" for r in result["indicators"])
    assert result["issues"] == []


@pytest.mark.parametrize("status,label", [("AtRisk", "有风险"), ("Suspended", "已停用")])
def test_fresh_explicit_platform_risk_is_attention_even_without_metric_breaches(warehouse, status, label):
    db, _, _, row = warehouse
    row.ahr_status = status
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "ATTENTION"
    assert result["source_status_label"] == label
    assert result["issues"] == []


@pytest.mark.parametrize("status,label", [("GREAT", "评级良好"), ("NOT_APPLICABLE", "不适用"),
    ("new_vendor_status", "未识别"), (None, "未提供")])
def test_unconfirmed_ratings_or_unknown_status_do_not_infer_health_from_score(warehouse, status, label):
    db, _, _, row = warehouse
    row.ahr_status, row.ahr_score = status, 1000
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "UNCONFIRMED"
    assert result["source_status_label"] == label
    assert result["confirmation_reason"] == "源评级口径待核实"


def test_zero_and_list_wrapped_standards_are_comparable_without_using_saved_breaches(warehouse):
    db, _, _, row = warehouse
    row.standards = {"items": [{"indicator": field, "standard": value} for field, value in STANDARDS.items()]}
    row.indicators = {**VALUES, "order_with_defect": "0.00%", "late_shipment": "5%"}
    assert row.breaches == []  # Observation mode deliberately stores this empty.
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "ATTENTION"
    fields = {item["field"]: item for item in result["indicators"]}
    assert fields["order_with_defect"]["value"] == 0
    assert fields["order_with_defect"]["status"] == "WITHIN_STANDARD"
    assert fields["late_shipment"]["status"] == "BREACHED"
    assert result["issues"] == ["迟发率5%高于平台标准4%"]


@pytest.mark.parametrize("value", [None, "--", "bad", "NaN", "inf", float("nan"),
    float("inf"), -1, 101, True, 10 ** 1000])
def test_invalid_percentage_observation_never_passes_as_in_range(warehouse, value):
    db, _, _, row = warehouse
    row.indicators = {**VALUES, "order_with_defect": value}
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "UNCONFIRMED"
    first = result["indicators"][0]
    assert first["value"] is None and first["status"] == "INSUFFICIENT_DATA"
    assert result["issues"] == []


@pytest.mark.parametrize("value", [None, "NaN", "inf", -1, 101, 10 ** 1000])
def test_invalid_or_missing_platform_standard_keeps_health_unconfirmed(warehouse, value):
    db, _, _, row = warehouse
    row.standards = {**STANDARDS, "order_with_defect": value}
    db.flush()
    result = snapshot(warehouse)
    assert result["status"] == "UNCONFIRMED"
    assert result["indicators"][0]["standard"] is None
    assert "数据不足" in result["indicator_summary"]


@pytest.mark.parametrize("field", ["order_with_defect", "valid_tracking"])
def test_exact_standard_boundary_does_not_guess_inclusive_operator(warehouse, field):
    db, _, _, row = warehouse
    row.indicators = {**VALUES, field: STANDARDS[field]}
    db.flush()
    result = snapshot(warehouse)
    item = next(r for r in result["indicators"] if r["field"] == field)
    assert item["status"] == "AT_THRESHOLD"
    assert result["status"] == "UNCONFIRMED"
    assert result["issues"] == []
    assert "边界" in result["indicator_summary"]


def test_archived_pending_notices_remain_observations_even_after_empty_new_list(warehouse):
    db, _, _, _ = warehouse
    for key, label in [("old-pending", "PENDING"), ("handled", "PROCESSED")]:
        db.add(PerformanceNotice(store_id="101", notice_key=key, status_label=label,
            last_seen=datetime(2020, 1, 1)))
    db.add(PerformanceSyncState(store_id="101", dataset="performance_notices", status="FRESH",
        source_updated_at="2020-01-01 00:00:00", captured_at=NOW.replace(tzinfo=None), record_count=0))
    db.flush()
    result = snapshot(warehouse)
    assert result["pending_notice_observations"] == 1
    assert result["notice_freshness_status"] == "STALE"


def test_snapshot_is_read_only_even_when_the_callers_session_has_dirty_objects(warehouse):
    db, store, _, _ = warehouse
    risk = AnomalyEvent(dedupe_key="existing-risk", store_id="101", rule="account_health_at_risk",
        entity_id="101", severity="P1", status="NEW", evaluation_scope="101:store_performance",
        first_seen=NOW.replace(tzinfo=None), last_seen=NOW.replace(tzinfo=None),
        metrics={"ahr_status": "AtRisk"}, reason="已有风险")
    db.add(risk)
    db.flush()
    before = (risk.status, risk.last_seen, dict(risk.metrics), risk.reason, risk.recovered_at)
    store.name = "调用方尚未保存的名称"
    pending = set(db.dirty)
    writes = []

    def watch(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(db.bind, "before_cursor_execute", watch)
    try:
        result = snapshot(warehouse)
    finally:
        event.remove(db.bind, "before_cursor_execute", watch)
    assert result["status"] == "HEALTHY"
    assert writes == [] and set(db.dirty) == pending
    assert (risk.status, risk.last_seen, risk.metrics, risk.reason, risk.recovered_at) == before
    with db.no_autoflush:
        assert db.scalar(select(func.count(AnomalyEvent.id))) == 1
        assert db.scalar(select(func.count(NotificationOutbox.id))) == 0


def test_naive_clock_is_rejected(warehouse):
    with pytest.raises(ValueError, match="aware"):
        snapshot(warehouse, now=NOW.replace(tzinfo=None))
