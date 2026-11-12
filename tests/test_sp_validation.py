from copy import deepcopy
from datetime import date
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.lingxing.sp_validation import (
    DAILY_PATH, HOUR_ENDPOINTS, MASTER_PATH, collect_sp_validation,
)
from jx_alert.storage.models import DailyMetric, RawSnapshot


DAY = "2026-11-11"
CID, SID, PROFILE = 141469378857743, 5174, 1251791933842067


def report_row(**changes):
    return {
        "campaign_id": CID, "profile_id": PROFILE, "report_date": DAY,
        "cost": "0.30", "clicks": 3, "impressions": 30, "orders": 1,
        "sales": "9.99", **changes,
    }


def hour_row(**changes):
    return report_row(**{"hour": 0, "group_id": 10, "ad_id": 20, "targeting_id": 30, **changes})


def response(rows, *, total=None, **changes):
    return {
        "code": 0, "data": rows, "total": len(rows) if total is None else total,
        "request_id": "evidence-request-id", "next_token": None, **changes,
    }


class FakeClient:
    access_token = "test-secret-access-token"
    app_key = "test-secret-app-key"
    app_id = "test-secret-app-id"

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def _request(self, method, path, *, json_body, advertising):
        assert method == "POST" and advertising is True
        self.calls.append((path, deepcopy(json_body)))
        value = self.overrides.get(path)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return deepcopy(value(json_body))
        if value is not None:
            return deepcopy(value)
        if path == MASTER_PATH:
            return response([{
                "campaign_id": CID, "profile_id": PROFILE, "name": "Target campaign",
                "state": "enabled", "start_date": "2026-11-09", "end_date": None,
            }])
        if path == DAILY_PATH:
            return response([report_row(report_date=json_body["report_date"])])
        return response([hour_row(report_date=json_body["report_date"])])


@pytest.fixture
def db():
    # This test session connects only to an explicit, transient SQLite database.
    engine = create_engine("sqlite:///:memory:")
    RawSnapshot.__table__.create(engine)
    DailyMetric.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def collect(db, client=None, **kwargs):
    return collect_sp_validation(
        client or FakeClient(), db, sid=SID, campaign_id=CID,
        report_dates=[DAY], request_interval=0, **kwargs,
    )


def test_scalar_requests_capture_each_response_and_compare_dimensions_separately(db):
    client = FakeClient()
    result = collect(db, client)
    assert result["status"] == "COMPLETE"
    day = result["days"][0]
    assert day["daily"]["records"][0]["campaign_name"] == "Target campaign"
    assert day["daily"]["metrics"]["cost"] == "0.30"
    assert {entry["status"] for entry in day["reconciliation"].values()} == {"EQUAL"}
    assert all(entry["delta_hour_minus_daily"]["cost"] == "0.00"
               for entry in day["reconciliation"].values())
    for path, request in client.calls:
        assert "campaign_ids" not in request
        if path in {DAILY_PATH, MASTER_PATH}:
            assert request["sid"] == SID
            assert request["offset"] == 0 and request["length"] == 100
        else:
            assert request["campaign_id"] == CID
            assert "sid" not in request
    assert dict(client.calls)[HOUR_ENDPOINTS["ad"][0]]["agg_dimension"] == "ad"
    assert dict(client.calls)[HOUR_ENDPOINTS["target"][0]]["agg_dimension"] == "target"
    snapshots = db.scalars(select(RawSnapshot).order_by(RawSnapshot.id)).all()
    assert len(snapshots) == 8  # six responses, one day result, one run result
    assert {row.payload["run_id"] for row in snapshots} == {result["run_id"]}
    assert all(row.captured_at is not None for row in snapshots)
    assert snapshots[1].payload["response"]["request_id"] == "evidence-request-id"
    assert snapshots[1].payload["request"]["show_detail"] == 1


def test_full_cursor_pagination_finds_target_on_second_page(db):
    first = report_row(campaign_id=CID + 1)
    client = FakeClient({DAILY_PATH: lambda request: (
        response([report_row()], total=2)
        if "next_token" in request else response([first], total=2, next_token="page-2")
    )})
    day = collect(db, client, length=1)["days"][0]
    assert day["daily"]["status"] == "MATCHED"
    assert day["daily"]["row_count"] == 2
    assert day["daily"]["matched_row_count"] == 1
    assert len(day["daily"]["records"]) == 2
    requests = [request for path, request in client.calls if path == DAILY_PATH]
    assert requests[1]["next_token"] == "page-2" and "offset" not in requests[1]


@pytest.mark.parametrize("rows,status,metrics", [
    ([], "NO_RECORDS", None),
    ([report_row(campaign_id=CID + 1)], "UNMATCHED", None),
    ([report_row(cost=0)], "ZERO_SPEND", "0"),
])
def test_no_record_unmatched_and_explicit_zero_are_distinct(db, rows, status, metrics):
    day = collect(db, FakeClient({DAILY_PATH: response(rows)}))["days"][0]
    assert day["daily"]["status"] == status
    if metrics is None:
        assert day["daily"]["metrics"] is None
        assert day["reconciliation"]["campaign"]["status"] == "UNAVAILABLE"
        assert day["reconciliation"]["campaign"]["delta_hour_minus_daily"] is None
    else:
        assert day["daily"]["metrics"]["cost"] == metrics
        assert day["reconciliation"]["campaign"]["status"] == "DIFFERENT"


@pytest.mark.parametrize("failure,code", [
    (response([report_row()], total=2), "INCOMPLETE_PAGINATION"),
    (response([report_row(), report_row()]), "DUPLICATE_DAILY_CAMPAIGN"),
    (response([report_row(report_date="2026-11-10")]), "UNEXPECTED_REPORT_DATE"),
    (response([report_row(profile_id=PROFILE + 1)]), "UNEXPECTED_PROFILE"),
    (response([report_row(campaign_id=1.25)]), "INVALID_IDENTIFIER"),
    (response([report_row(cost=None)]), "MISSING_OR_INVALID_METRIC"),
    (response([report_row(cost="NaN")]), "MISSING_OR_INVALID_METRIC"),
    (response([report_row(cost=float("inf"))]), "MISSING_OR_INVALID_METRIC"),
    (response([report_row(clicks=1.5)]), "INVALID_COUNT_METRIC"),
    (response([], total=True), "INVALID_TOTAL"),
    (response([], code=False), "UNSUCCESSFUL_RESPONSE"),
])
def test_invalid_daily_data_keeps_raw_evidence_and_never_creates_zero(db, failure, code):
    result = collect(db, FakeClient({DAILY_PATH: failure}))
    assert result["status"] == "PARTIAL"
    day = result["days"][0]
    assert day["daily"]["status"] == "INVALID_RESPONSE"
    assert day["daily"]["error_code"] == code
    assert day["daily"]["metrics"] is None
    assert day["reconciliation"]["campaign"]["status"] == "UNAVAILABLE"
    raw = db.scalars(select(RawSnapshot).where(
        RawSnapshot.source == "lingxing_sp_validation_page",
    )).all()
    assert len(raw) == 6
    assert day["hours"]["campaign"]["status"] == "MATCHED"


def test_changed_total_between_pages_is_failed_not_partial_success(db):
    client = FakeClient({DAILY_PATH: lambda request: (
        response([report_row()], total=2)
        if "next_token" in request else response(
            [report_row(campaign_id=CID + 1)], total=3, next_token="second",
        )
    )})
    daily = collect(db, client)["days"][0]["daily"]
    assert daily["status"] == "INVALID_RESPONSE"
    assert daily["error_code"] == "TOTAL_CHANGED_DURING_PAGINATION"
    assert daily["metrics"] is None


def test_repeated_cursor_and_max_pages_fail_closed(db):
    next_id = iter(range(100, 200))
    client = FakeClient({DAILY_PATH: lambda request: response(
        [report_row(campaign_id=next(next_id))], total=50, next_token="same",
    )})
    daily = collect(db, client)["days"][0]["daily"]
    assert daily["error_code"] == "REPEATED_NEXT_TOKEN"
    daily = collect(db, client, max_pages=1)["days"][0]["daily"]
    assert daily["error_code"] == "MAX_PAGES_EXCEEDED"


@pytest.mark.parametrize("dimension,identity", [
    ("campaign", None), ("ad_group", "group_id"),
    ("ad", "ad_id"), ("target", "targeting_id"),
])
def test_duplicate_hour_identity_is_rejected(db, dimension, identity):
    result = collect(db, FakeClient({
        HOUR_ENDPOINTS[dimension][0]: response([hour_row(), hour_row()]),
    }))
    hourly = result["days"][0]["hours"][dimension]
    assert hourly["status"] == "INVALID_RESPONSE"
    assert hourly["error_code"] == "DUPLICATE_HOUR_ENTITY"
    assert hourly["metrics"] is None


@pytest.mark.parametrize("dimension,identity", [
    ("ad_group", "group_id"), ("ad", "ad_id"), ("target", "targeting_id"),
])
def test_missing_hour_identity_is_rejected(db, dimension, identity):
    row = hour_row()
    row.pop(identity)
    hourly = collect(db, FakeClient({HOUR_ENDPOINTS[dimension][0]: response([row])}))[
        "days"][0]["hours"][dimension]
    assert hourly["status"] == "INVALID_RESPONSE"
    assert hourly["error_code"] == "INVALID_IDENTIFIER"


@pytest.mark.parametrize("hour_response,status,code", [
    (response([]), "NO_RECORDS", None),
    (response([hour_row(campaign_id=CID + 1)]), "INVALID_RESPONSE", "UNEXPECTED_CAMPAIGN"),
    (response([hour_row(hour=24)]), "INVALID_RESPONSE", "INVALID_HOUR"),
    (response([hour_row(profile_id=PROFILE + 1)]), "INVALID_RESPONSE", "UNEXPECTED_PROFILE"),
    (response([hour_row()], total=2), "INVALID_RESPONSE", "INCOMPLETE_HOUR_RESPONSE"),
    (response([hour_row()], next_token="more"), "INVALID_RESPONSE", "UNEXPECTED_HOUR_PAGINATION"),
])
def test_hour_scope_and_completeness_are_required(db, hour_response, status, code):
    day = collect(db, FakeClient({HOUR_ENDPOINTS["campaign"][0]: hour_response}))["days"][0]
    assert day["hours"]["campaign"]["status"] == status
    if code:
        assert day["hours"]["campaign"]["error_code"] == code
    assert day["hours"]["campaign"]["metrics"] is None
    assert day["reconciliation"]["campaign"]["status"] == "UNAVAILABLE"


def test_decimal_aggregation_uses_each_dimension_and_retains_differences(db):
    rows = [
        hour_row(cost=0.1, clicks=1, impressions=10, orders=0, sales=0),
        hour_row(hour=1, cost=0.2, clicks=2, impressions=20, orders=1, sales="9.99"),
    ]
    client = FakeClient({
        HOUR_ENDPOINTS["campaign"][0]: response(rows),
        HOUR_ENDPOINTS["target"][0]: response([hour_row(cost="0.31")]),
    })
    day = collect(db, client)["days"][0]
    assert day["hours"]["campaign"]["metrics"]["cost"] == "0.3"
    assert day["reconciliation"]["campaign"]["status"] == "EQUAL"
    assert day["reconciliation"]["target"]["status"] == "DIFFERENT"
    assert day["reconciliation"]["target"]["delta_hour_minus_daily"]["cost"] == "0.01"
    assert day["daily"]["metrics"]["cost"] == "0.30"


def test_failed_request_never_leaks_exception_or_known_credentials(db):
    client = FakeClient({
        HOUR_ENDPOINTS["ad"][0]: RuntimeError(
            "https://example.invalid/?access_token=test-secret-access-token",
        ),
        DAILY_PATH: response([report_row()], authorization="unexpected-secret",
                             meta={"app_key": "test-secret-app-key"},
                             message="token test-secret-access-token"),
    })
    result = collect(db, client)
    failed = result["days"][0]["hours"]["ad"]
    assert failed["status"] == "REQUEST_FAILED" and failed["metrics"] is None
    assert failed["error_type"] == "RuntimeError"
    serialized = json.dumps(result) + json.dumps([
        row.payload for row in db.scalars(select(RawSnapshot)).all()
    ])
    for secret in (client.access_token, client.app_key, client.app_id,
                   "unexpected-secret", "example.invalid"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_backfill_appends_versions_without_touching_daily_metrics_or_committing(db):
    db.add(DailyMetric(
        store_id=str(SID), metric_date=date.fromisoformat(DAY), dimension="campaign_sp",
        entity_id=str(CID), metric_name="spend", value=123, source="existing",
    ))
    db.commit()
    first = collect(db)
    count = db.scalar(select(func.count()).select_from(RawSnapshot))
    second = collect(db, FakeClient({DAILY_PATH: response([report_row(cost="0.40")])}))
    assert first["run_id"] != second["run_id"]
    assert db.scalar(select(func.count()).select_from(RawSnapshot)) == count * 2
    assert first["days"][0]["daily"]["metrics"]["cost"] == "0.30"
    assert second["days"][0]["daily"]["metrics"]["cost"] == "0.40"
    assert db.scalar(select(DailyMetric)).value == 123
    db.rollback()
    assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 0
    assert db.scalar(select(DailyMetric)).value == 123


def test_master_failure_does_not_prevent_readonly_report_capture(db):
    client = FakeClient({MASTER_PATH: RuntimeError("private")})
    result = collect(db, client)
    assert result["status"] == "PARTIAL"
    assert result["master"]["status"] == "REQUEST_FAILED"
    assert result["days"][0]["daily"]["status"] == "MATCHED"
    assert result["days"][0]["daily"]["records"][0]["campaign_name"] is None


def test_date_window_is_sorted_deduplicated_and_bound_before_requests(db):
    client = FakeClient()
    result = collect_sp_validation(
        client, db, sid=SID, campaign_id=CID,
        report_dates=[DAY, "2026-11-10", DAY], request_interval=0,
    )
    assert [day["report_date"] for day in result["days"]] == ["2026-11-10", DAY]
    assert len(client.calls) == 11
    empty_client = FakeClient()
    with pytest.raises(ValueError):
        collect_sp_validation(empty_client, db, sid=SID, campaign_id=CID, report_dates=["20261111"])
    with pytest.raises(ValueError):
        collect_sp_validation(empty_client, db, sid=SID, campaign_id=CID, report_dates=[])
    assert empty_client.calls == []
