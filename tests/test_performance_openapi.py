from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import AnomalyEvent, PerformanceNotice, PerformanceSyncState, RawSnapshot, StorePerformance
from jx_alert.lingxing.performance_openapi import _fetch_all, _freshness_status, _notice_status, collect_store_performance
from jx_alert.lingxing.client import LingxingClient
import jx_alert.analysis.service as anomaly_service
import jx_alert.lingxing.performance_openapi as performance_module
from jx_alert.notifications.outbox import NotificationOutbox


class FakePerformanceClient:
    def __init__(self):
        self.calls = []

    def store_performance_list(self, payload):
        self.calls.append(("list", payload))
        return {"code": 0, "total": 1, "data": [{
            "sid": "101", "pull_date": "2026-11-10",
            "update_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "ahr_score": 245, "ahr_status": "Healthy",
            "order_with_defect": 0.5, "late_shipment": 0.2,
        }]}

    def store_performance_detail(self, payload):
        self.calls.append(("detail", payload))
        return {"code": 0, "data": {"standard": {
            "order_with_defect": 1.0, "late_shipment": 4.0,
        }, "detail": "sanitized"}}

    def performance_notice_list(self, payload):
        self.calls.append(("notices", payload))
        return {"code": 0, "total": 1, "data": {
            "lastUpdateDate": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "list": [{
                "notice_id": "notice-1", "subject": "Policy notification",
                "status": 1, "date": "2026-11-10 10:00:00", "isRead": 0,
            }],
        }}


def _settings(timezone_name="UTC"):
    return SimpleNamespace(
        lingxing_performance_page_size=200,
        lingxing_performance_max_pages=10,
        lingxing_performance_source_timezone=timezone_name,
        lingxing_performance_max_age_hours=8,
    )


def test_freshness_requires_configured_timezone_and_real_source_timestamp():
    assert _freshness_status("2026-11-10 10:00:00", _settings("")) == "NOT_CONFIGURED"
    assert _freshness_status(None, _settings("UTC")) == "MISSING"
    assert _freshness_status(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), _settings("UTC")
    ) == "FRESH"


def test_notice_status_zero_is_not_treated_as_handled():
    assert _notice_status(0) == (0, "UNKNOWN")
    assert _notice_status(1) == (1, "PENDING")
    assert _notice_status(2) == (2, "PROCESSED")
    assert _notice_status(3) == (3, "NO_ACTION")
    assert _notice_status("unexpected") == (None, "UNKNOWN")


def test_fetch_all_detects_incomplete_pagination():
    pages = iter([{"data": [{"id": 1}], "total": 2}, {"data": [], "total": 2}])
    with pytest.raises(RuntimeError, match="pagination incomplete"):
        _fetch_all(lambda offset, length: next(pages), page_size=1,
                   max_pages=5, label="fixture")


def test_collect_performance_persists_detail_notices_and_raw_responses():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    client = FakePerformanceClient()
    with Session() as db:
        result = collect_store_performance(
            client, db, stores_response={"data": [{"sid": 101, "name": "Store A"}]},
            settings=_settings(),
        )
        assert result["status"] == "SUCCEEDED"
        assert result["performance_rows"] == 1
        assert result["notice_rows"] == 1
        performance = db.scalar(select(StorePerformance))
        assert performance.ahr_score == 245
        assert performance.standards["order_with_defect"] == 1.0
        assert performance.freshness_status == "FRESH"
        notice = db.scalar(select(PerformanceNotice))
        assert notice.status_label == "PENDING"
        assert notice.external_id == "notice-1"
        states = db.scalars(select(PerformanceSyncState)).all()
        assert {row.dataset for row in states} == {"store_performance", "performance_notices"}
        assert all(row.status == "FRESH" for row in states)
        raw = db.scalars(select(RawSnapshot)).all()
        assert len(raw) == 3
        assert {row.source for row in raw} == {
            "lingxing_store_performance_list", "lingxing_store_performance_detail",
            "lingxing_performance_notice_list",
        }
        events = db.scalars(select(AnomalyEvent)).all()
        assert any(row.rule == "performance_notice_pending" for row in events)
        notice_call = next(payload for kind, payload in client.calls if kind == "notices")
        assert "status" not in notice_call and "isRead" not in notice_call
        detail_call = next(payload for kind, payload in client.calls if kind == "detail")
        assert detail_call["pullDate"] == "2026-11-10"


def test_missing_timezone_never_recovers_freshness_event():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_store_performance(
            FakePerformanceClient(), db,
            stores_response={"data": [{"sid": 101, "name": "Store A"}]},
            settings=_settings(""),
        )
        assert result["freshness"][0]["performance_freshness"] == "NOT_CONFIGURED"
        assert result["freshness"][0]["notice_freshness"] == "NOT_CONFIGURED"
        events = db.scalars(select(AnomalyEvent)).all()
        assert any("freshness" in row.rule for row in events)
        assert not any(row.status == "RECOVERED" for row in events)


def test_client_uses_confirmed_read_only_performance_paths(monkeypatch):
    client = LingxingClient(base_url="https://example.invalid", app_id="id")
    calls = []
    monkeypatch.setattr(client, "_request", lambda method, path, **kwargs:
                        calls.append((method, path, kwargs)) or {"code": 0, "data": []})
    client.store_performance_list({"sids": "101"})
    client.store_performance_detail({"sid": 101, "pullDate": "2026-11-10"})
    client.performance_notice_list({"sid": 101, "offset": 0, "length": 200})
    assert [call[1] for call in calls] == [
        "/basicOpen/customerService/storeTarget/list",
        "/basicOpen/customerService/storeTarget/detail",
        "/basicOpen/customerService/performanceNotice/list",
    ]


@pytest.mark.parametrize("configured_page_size", [50, 200])
def test_notice_endpoint_uses_verified_page_size_without_truncating(configured_page_size):
    class PagedNoticeClient(FakePerformanceClient):
        def performance_notice_list(self, payload):
            self.calls.append(("notices", payload))
            assert payload["length"] <= 100
            start = payload["offset"]
            rows = [{"notice_id": f"notice-{i}", "subject": "Historical notice",
                     "status": 3, "date": "2026-11-10 10:00:00"}
                    for i in range(start, min(start + payload["length"], 102))]
            return {"code": 0, "data": {"list": rows, "total": 102,
                    "lastUpdateDate": "2026-11-10 10:00:00"}}

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    client = PagedNoticeClient()
    settings = _settings("")
    settings.lingxing_performance_page_size = configured_page_size
    with sessionmaker(bind=engine)() as db:
        result = collect_store_performance(
            client, db, stores_response={"data": [{"sid": 101}]}, settings=settings,
        )
        assert result["status"] == "SUCCEEDED_WITH_WARNINGS"
        assert result["notice_rows"] == 102
        assert len(db.scalars(select(PerformanceNotice)).all()) == 102
        notices = [payload for kind, payload in client.calls if kind == "notices"]
        step = min(configured_page_size, 100)
        assert [payload["offset"] for payload in notices] == list(range(0, 102, step))
        assert next(payload for kind, payload in client.calls if kind == "list")["length"] == configured_page_size


_NOW = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
_BUSINESS_RULES = {
    "account_health_at_risk", "performance_indicator_breached", "performance_notice_pending",
}
_NON_FRESH = ("UNKNOWN", "NOT_CONFIGURED", "STALE", "FUTURE", "MISSING")


class SourceTimeClient(FakePerformanceClient):
    def __init__(self, timestamp, *, risk=True, notice_status=1):
        super().__init__()
        self.timestamp, self.risk, self.notice_status = timestamp, risk, notice_status

    def store_performance_list(self, payload):
        response = super().store_performance_list(payload)
        row = response["data"][0]
        row["update_date"] = self.timestamp
        row["ahr_status"] = "AtRisk" if self.risk else "Healthy"
        row["order_with_defect"] = 2.5 if self.risk else 0.5
        return response

    def performance_notice_list(self, payload):
        response = super().performance_notice_list(payload)
        response["data"]["lastUpdateDate"] = self.timestamp
        response["data"]["list"][0]["status"] = self.notice_status
        return response


@pytest.fixture
def freshness_db(monkeypatch):
    monkeypatch.setattr(performance_module, "_utcnow", lambda: _NOW)
    # Exercise real durable enqueue without loading any deployment credentials.
    monkeypatch.setattr(anomaly_service, "get_settings",
                        lambda: SimpleNamespace(feishu_transport="webhook"))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


def _unfresh_source(state, monkeypatch):
    settings = _settings("") if state == "NOT_CONFIGURED" else _settings()
    timestamp = {
        "STALE": _NOW - timedelta(days=2), "FUTURE": _NOW + timedelta(hours=1),
        "MISSING": None,
    }.get(state, _NOW)
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat()
    if state == "UNKNOWN":
        # Gate unexpected/unknown states too, without assigning a timezone or
        # inventing an interpretation for an offset-free provider timestamp.
        monkeypatch.setattr(performance_module, "_freshness_status", lambda *a, **k: "UNKNOWN")
    return timestamp, settings


def _collect_source(db, client, settings=None):
    return collect_store_performance(
        client, db, stores_response={"data": [{"sid": 101, "name": "Store A"}]},
        settings=settings or _settings(),
    )


def _business_events(db):
    return db.scalars(select(AnomalyEvent).where(AnomalyEvent.rule.in_(_BUSINESS_RULES))).all()


@pytest.mark.parametrize("state", _NON_FRESH)
def test_unfresh_health_and_pending_data_persist_without_business_alerts(
    freshness_db, monkeypatch, state,
):
    db = freshness_db
    timestamp, settings = _unfresh_source(state, monkeypatch)
    result = _collect_source(db, SourceTimeClient(timestamp), settings)

    assert result["status"] == "SUCCEEDED_WITH_WARNINGS"
    assert result["freshness"][0]["performance_freshness"] == state
    assert result["freshness"][0]["notice_freshness"] == state
    performance = db.scalar(select(StorePerformance))
    assert performance.ahr_status == "AtRisk"
    assert performance.indicators["order_with_defect"] == 2.5
    assert performance.freshness_status == state
    assert db.scalar(select(PerformanceNotice)).status_label == "PENDING"
    assert len(db.scalars(select(RawSnapshot)).all()) == 3
    assert _business_events(db) == []
    events = db.scalars(select(AnomalyEvent)).all()
    assert len(events) == 2 and all("freshness" in event.rule for event in events)
    outbox = db.scalars(select(NotificationOutbox)).all()
    assert len(outbox) == 2
    assert all("freshness" in row.payload["text"] for row in outbox)


@pytest.mark.parametrize("state", _NON_FRESH)
def test_unfresh_healthy_and_handled_observations_cannot_recover_existing_risks(
    freshness_db, monkeypatch, state,
):
    db = freshness_db
    _collect_source(db, SourceTimeClient(_NOW.isoformat()))
    existing = _business_events(db)
    assert {event.rule for event in existing} == _BUSINESS_RULES
    before = {event.id: (event.status, event.last_seen, dict(event.metrics), event.reason)
              for event in existing}
    timestamp, settings = _unfresh_source(state, monkeypatch)

    _collect_source(db, SourceTimeClient(timestamp, risk=False, notice_status=2), settings)

    assert {event.id: (event.status, event.last_seen, event.metrics, event.reason)
            for event in _business_events(db)} == before
    assert all(event.recovered_at is None for event in existing)
    assert db.scalar(select(StorePerformance)).ahr_status == "Healthy"
    assert db.scalar(select(PerformanceNotice)).status_label == "PROCESSED"
    assert len(db.scalars(select(RawSnapshot)).all()) == 6
    assert len(db.scalars(select(NotificationOutbox)).all()) == 5  # three risks + two data warnings


def test_fresh_healthy_and_handled_sources_still_recover_business_risks(freshness_db):
    db = freshness_db
    first = _collect_source(db, SourceTimeClient(_NOW.isoformat()))
    assert first["status"] == "SUCCEEDED"
    assert len(_business_events(db)) == 3

    second = _collect_source(
        db, SourceTimeClient(_NOW.isoformat(), risk=False, notice_status=2),
    )

    assert second["status"] == "SUCCEEDED"
    assert all(event.status == "RECOVERED" for event in _business_events(db))
    assert all(event.recovered_at is not None for event in _business_events(db))
    assert db.scalar(select(PerformanceNotice)).status_label == "PROCESSED"
    assert len(db.scalars(select(NotificationOutbox)).all()) == 6


@pytest.mark.parametrize("later_state", ["STALE", "FUTURE", "MISSING"])
def test_notice_freshness_checks_all_pages_before_any_business_evaluation(
    freshness_db, monkeypatch, later_state,
):
    db = freshness_db
    _collect_source(db, SourceTimeClient(_NOW.isoformat(), risk=False))
    pending = next(event for event in _business_events(db)
                   if event.rule == "performance_notice_pending")
    before = (pending.status, pending.last_seen, dict(pending.metrics))
    later_timestamp, settings = _unfresh_source(later_state, monkeypatch)
    settings.lingxing_performance_page_size = 1

    class MixedPagesClient(SourceTimeClient):
        def performance_notice_list(self, payload):
            response = super().performance_notice_list(payload)
            offset = payload["offset"]
            response["total"] = 2
            response["data"]["lastUpdateDate"] = self.timestamp if offset == 0 else later_timestamp
            response["data"]["list"] = [{
                "notice_id": f"notice-{offset + 1}", "subject": "Policy notification",
                "status": 2 if offset == 0 else 1,
            }]
            return response

    result = _collect_source(
        db, MixedPagesClient(_NOW.isoformat(), risk=False), settings,
    )

    assert result["freshness"][0]["notice_freshness"] == later_state
    assert (pending.status, pending.last_seen, pending.metrics) == before
    assert pending.recovered_at is None
    assert len(_business_events(db)) == 1
    notices = db.scalars(select(PerformanceNotice).order_by(PerformanceNotice.external_id)).all()
    assert [notice.status_label for notice in notices] == ["PROCESSED", "PENDING"]
    assert len(db.scalars(select(RawSnapshot)).all()) == 7
