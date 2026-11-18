from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert import hourly_source_fallback as fallback
from jx_alert.config import Settings
from jx_alert.lingxing import source_gateway
from jx_alert.lingxing.api_business import PRODUCT, FINANCE
from jx_alert.lingxing.business_dashboard import persist_business_cards
from jx_alert.lingxing.intraday_observation import SOURCE
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AnomalyEvent, Campaign, DailyMetric, DataCoverage, PerformanceSyncState, RawSnapshot, Store, StorePerformance,
)
from jx_alert.store_controls import get_store_controls, save_store_controls


NOW = datetime(2026, 11, 18, 0, tzinfo=timezone.utc)
TARGET = "2026-11-16"


class OfficialReader:
    def __init__(self):
        self.calls = []
        self.ads_fail = False
        self.inventory_fail = False

    def stores(self):
        if self.inventory_fail:
            raise RuntimeError("sensitive upstream detail")
        visible = [{"id": n} for n in (11, 12, 13, 14, 16)]
        return visible, {"code": 1, "data": {"code": 1, "list": visible}}

    def profiles(self):
        return [{"type": "seller", "sid": 11, "profile_id": 211, "real_status": 1}], {}

    def check_store(self, sid):
        assert sid == "11"

    def campaign_ads(self, sid, day, kind):
        self.calls.append(("ads", sid, day, kind))
        if self.ads_fail:
            raise RuntimeError("sensitive upstream detail")
        return [{"entity_id": "10", "profile_id": "211", "name": "活动",
                 "spend": 50, "clicks": 8, "impressions": 100}], []

    def call(self, tool, params):
        self.calls.append((tool, params))
        if tool == "customer_store_performance_list":
            return {"code": 1, "data": {"code": 1, "data": [{"sid": 11, "pull_date": "2026-11-17",
                "update_date": "2026-11-17 15:30:00", "ahr_score": 250, "ahr_status": "AtRisk"}]}}
        if tool == "customer_store_target_detail":
            return {"code": 1, "data": {"code": 1, "data": {"standard": {}}}}
        pytest.fail(f"Unexpected tool {tool}")


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    scope, zones = (tmp_path / name for name in ("scope.json", "zones.json"))
    ids = (11, 12, 13, 15, 16)  # 14 is out of scope and 15 isn't visible from MCP.
    scope.write_text(json.dumps({"store_ids": ids}), encoding="utf-8")
    zones.write_text(json.dumps({"stores": {str(sid): {"timezone": "America/Los_Angeles"}
        for sid in ids}}), encoding="utf-8")
    settings = Settings(_env_file=None, collection_scope_file=str(scope), store_timezones_file=str(zones),
        store_controls_file=str(tmp_path / "controls.json"), raw_dir=str(tmp_path / "raw"),
        lingxing_performance_enabled=True, lingxing_performance_observation_only=False)
    save_store_controls(settings, "13", False, True, get_store_controls(settings, "13")["revision"])
    with sessions.begin() as db:
        db.add_all([Store(sid=str(n), name=f"店铺{n}", status=2 if n == 12 else 1, has_ads_setting=1,
            timezone="America/Los_Angeles") for n in (11, 12, 13, 14, 15)])
        db.add(Campaign(store_id="11", campaign_type="sp", campaign_id="10", name="活动", state="paused"))
        db.add(PerformanceSyncState(store_id="11", dataset="performance_notices", status="FRESH", record_count=9))
        db.add(RawSnapshot(source="lingxing_seller_ad_accounts", payload={"rows": [
            {"type": "seller", "sid": 11, "profile_id": 211, "currency_code": "USD"}]}))
    reader = OfficialReader()
    monkeypatch.setattr(source_gateway, "OfficialReadSource", lambda settings: reader)
    monkeypatch.setattr(fallback, "mcp_credentials", lambda settings: ("fixture", "fixture"))
    monkeypatch.setattr(fallback, "_utcnow", lambda: NOW)
    yield settings, sessions, reader
    engine.dispose()


def test_morning_fallback_saves_only_independent_verified_observations(warehouse, monkeypatch):
    settings, sessions, reader = warehouse
    def collect_business(db, settings, visible, dates):
        assert [row["sid"] for row in visible["data"]] == [11]
        assert dates == {"11": TARGET}
        part = persist_business_cards(db, "11", TARGET, {"code": 200, "data": {
            "currencyCode": "USD", "current": {"volume": 7, "netAmount": 99},
            "chainCompareStartDate": "2026-11-15", "chainCompareEndDate": "2026-11-15",
            "chainCompare": {"volume": 4}}})
        return {"status": "COMPLETE", "partitions": [part], "failures": []}
    monkeypatch.setattr(fallback, "collect_business_for_day", collect_business)

    with sessions() as db:
        result = fallback.collect_fallback_observations(db, settings, now=NOW, morning=True)
        assert result["morning_dates"] == {"11": TARGET}
        assert result["api_business_status"] == "PARTIAL_FAILED"
        assert result["performance_status"] == "SUCCEEDED_WITH_WARNINGS"
        assert list(result["observations"]) == ["11"]
        snapshot = db.scalar(select(RawSnapshot).where(RawSnapshot.source == SOURCE))
        assert snapshot.payload["observation_date"] == "2026-11-17"
        assert snapshot.payload["sources"] == ["official_mcp_observation"]
        assert snapshot.payload["rows"][0]["_attribution_unconfirmed"] is True
        assert "orders" not in snapshot.payload["rows"][0]
        assert db.scalar(select(StorePerformance.ahr_score)) == 250
        assert db.scalar(select(PerformanceSyncState.record_count).where(
            PerformanceSyncState.dataset == "performance_notices")) == 9
        assert {fact.dimension for fact in db.scalars(select(DailyMetric))} == {"business_dashboard"}
        coverage = list(db.scalars(select(DataCoverage).where(DataCoverage.dimension.in_([PRODUCT, FINANCE]))))
        assert len(coverage) == 4 and all(row.status == "FAILED" for row in coverage)
        assert db.scalar(select(Campaign.state)) == "paused"
        assert db.get(Store, "12").status == 2 and db.get(Store, "16") is None
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert db.scalar(select(func.count()).select_from(AnomalyEvent)) == 0
    assert ("ads", "11", "2026-11-17", "sp") in reader.calls


@pytest.mark.parametrize("inventory_fail", [False, True])
def test_hourly_source_error_records_only_sanitized_metadata(warehouse, monkeypatch, inventory_fail):
    settings, sessions, reader = warehouse
    reader.ads_fail = True
    reader.inventory_fail = inventory_fail
    monkeypatch.setattr(fallback, "collect_business_for_day", lambda *a: pytest.fail("Not the morning slot"))
    with sessions() as db:
        result = fallback.collect_fallback_observations(db, settings, now=NOW)
        assert result["status"] == "PARTIAL_FAILED"
        assert not result["morning_dates"] and not result["observations"]
        assert result["failures"][0]["step"] == ("inventory" if inventory_fail else "intraday")
        assert "sensitive upstream" not in json.dumps(result)
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert db.scalar(select(func.count()).select_from(RawSnapshot).where(RawSnapshot.source == SOURCE)) == 0


def test_absent_scope_never_reaches_an_external_reader(warehouse, monkeypatch):
    settings, sessions, _ = warehouse
    settings = settings.model_copy(update={"collection_scope_file": settings.collection_scope_file + ".absent"})
    monkeypatch.setattr(fallback, "mcp_credentials", lambda settings: pytest.fail("Scope is required first"))
    with sessions() as db:
        assert fallback.collect_fallback_observations(db, settings, now=NOW)["status"] == "NO_EXPLICIT_SCOPE"


def test_empty_target_partition_does_not_become_a_morning_report(warehouse, monkeypatch):
    settings, sessions, reader = warehouse
    settings = settings.model_copy(update={"lingxing_performance_enabled": False})
    reader.ads_fail = True
    def collect_business(db, *args):
        db.add(DataCoverage(store_id="11", metric_date=date.fromisoformat(TARGET),
            dimension="business_dashboard", status="COMPLETE", row_count=0))
        db.commit()
        return {"status": "COMPLETE", "partitions": [{"store_id": "11", "dates": [TARGET]}]}
    monkeypatch.setattr(fallback, "collect_business_for_day", collect_business)
    with sessions() as db:
        result = fallback.collect_fallback_observations(db, settings, now=NOW, morning=True)
        assert not result["morning_dates"]
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
