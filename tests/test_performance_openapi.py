from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import AnomalyEvent, PerformanceNotice, PerformanceSyncState, RawSnapshot, StorePerformance
from jx_alert.lingxing.performance_openapi import _fetch_all, _freshness_status, _notice_status, collect_store_performance
from jx_alert.lingxing.client import LingxingClient


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
