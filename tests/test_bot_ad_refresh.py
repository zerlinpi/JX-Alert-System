"""Interactive repair leaves the original authorization and facts intact."""
from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert import bot_ad_refresh as repair
from jx_alert.config import Settings
from jx_alert.storage.db import Base
from jx_alert.storage.job_lock import database_job_lock
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot, Store
from jx_alert.store_controls import get_store_controls, save_store_controls


CLOCK = datetime(2026, 11, 19, 6, tzinfo=timezone.utc)
TARGET = date(2026, 11, 17)
MATURE = date(2026, 11, 10)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    scope = tmp_path / "scope.json"
    zones = tmp_path / "zones.json"
    scope.write_text(json.dumps({"store_ids": [101, 102, 103, 104, 106]}))
    zones.write_text(json.dumps({"stores": {
        sid: {"timezone": "America/Los_Angeles"} for sid in ("101", "102", "103", "104", "105", "106")
    }}))
    settings = Settings(_env_file=None, app_dry_run=False, scheduler_enabled=True, mcp_operations_enabled=True,
        collection_scope_file=str(scope), store_timezones_file=str(zones), store_controls_file=str(tmp_path / "controls.json"))
    monkeypatch.setattr(repair, "mcp_credentials", lambda settings: None)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Store(sid=sid, name=sid, status=2 if sid == "103" else 1, has_ads_setting=1)
                    for sid in ("101", "102", "103", "104", "105", "106")])
        db.commit()
        revision = get_store_controls(settings, "102")["revision"]
        save_store_controls(settings, "102", False, True, revision)
        yield db, settings
    engine.dispose()


class Provider:
    def __init__(self, *, empty=False):
        self.calls = []
        self.empty = empty

    def get_stores(self):
        return {"data": [{"sid": int(sid), "status": 2 if sid == "106" else 1, "has_ads_setting": 1}
                         for sid in ("101", "102", "103", "104", "105", "106")]}

    def seller_ad_accounts(self, *, max_pages):
        assert max_pages == 12
        return [{"sid": 101, "profile_id": "99", "type": "seller", "currency_code": "USD"}]

    def iter_advertising_report(self, kind, payload, *, max_pages):
        assert max_pages == 12
        self.calls.append((str(payload["sid"]), kind, payload["report_date"]))
        if not self.empty:
            yield {"campaign_id": "200", "profile_id": "99", "report_date": payload["report_date"],
                   "cost": 120, "sales": 40, "clicks": 30, "impressions": 3500, "orders": 1, "units": 1}


def run(warehouse, provider, *, kinds=("sp",), on=TARGET, reader=lambda sid: sid != "104"):
    db, settings = warehouse
    stores = list(db.scalars(select(Store)))
    return repair.refresh_ad_evidence(db, settings, stores, {s.sid: on for s in stores}, ad_types=kinds,
                                      now=CLOCK, may_collect=reader, client_factory=lambda: provider)


def test_repair_respects_both_permissions_scope_active_state_and_limits(warehouse):
    provider = Provider()
    result = run(warehouse, provider, kinds=("sp", "sb", "sd"))
    assert result["planned"] == 8
    assert 0 < result["refreshed"] == len(provider.calls) <= 8
    assert {sid for sid, _, _ in provider.calls} == {"101"}
    assert {kind for _, kind, _ in provider.calls} == {"sp", "sb", "sd"}
    db, _ = warehouse
    assert set(db.scalars(select(DailyMetric.currency).where(
        DailyMetric.store_id == "101", DailyMetric.metric_name == "spend"))) == {"USD"}


def test_successful_empty_keeps_existing_provisional_fact_and_deduplicates_retry(warehouse):
    db, _ = warehouse
    db.add(DataCoverage(store_id="101", dimension="campaign_sp", metric_date=MATURE,
        status="COMPLETE", row_count=1, completed_at=datetime(2026, 11, 11)))
    db.add(DailyMetric(store_id="101", dimension="campaign_sp", metric_date=MATURE, entity_id="200",
        metric_name="spend", value=25, currency="USD", source="lingxing_ads_sp"))
    db.commit()
    provider = Provider(empty=True)
    result = run(warehouse, provider, reader=lambda sid: sid == "101")
    assert result["empty"] == len(provider.calls) == 4
    assert db.scalar(select(DailyMetric.value)) == 25
    assert db.scalar(select(DataCoverage.row_count)) == 1
    assert run(warehouse, provider, reader=lambda sid: sid == "101")["attempted"] == 0
    assert len(provider.calls) == 4


def test_coverage_without_source_snapshot_is_repaired(warehouse):
    db, _ = warehouse
    captured = datetime(2026, 11, 18, 12)
    for on in (TARGET, MATURE):
        db.add(DataCoverage(store_id="101", dimension="campaign_sp", metric_date=on,
            status="COMPLETE", row_count=1, completed_at=captured))
        for metric in ("spend", "sales"):
            db.add(DailyMetric(store_id="101", dimension="campaign_sp", metric_date=on, entity_id="200",
                metric_name=metric, value=25, currency="USD", source="lingxing_ads_sp"))
    db.add(RawSnapshot(source="lingxing_ads_sp_campaign_report", store_id="101",
        captured_at=captured, payload={"report_date": TARGET.isoformat(), "rows": [{"campaign_id": "200"}]}))
    db.commit()
    provider = Provider()
    assert run(warehouse, provider, reader=lambda sid: sid == "101")["refreshed"] == 3
    assert ("101", "sp", MATURE.isoformat()) in provider.calls
    assert ("101", "sp", TARGET.isoformat()) not in provider.calls


def test_profile_failure_cannot_remove_currency_from_existing_fact(warehouse):
    db, _ = warehouse
    db.add(DailyMetric(store_id="101", dimension="campaign_sp", metric_date=MATURE, entity_id="200",
        metric_name="spend", value=25, currency="USD", source="lingxing_ads_sp"))
    db.commit()

    class MissingCurrency(Provider):
        def seller_ad_accounts(self, *, max_pages):
            raise RuntimeError("inventory temporarily unavailable")

    assert run(warehouse, MissingCurrency(), reader=lambda sid: sid == "101")["failed"] == 4
    remaining = db.scalar(select(DailyMetric))
    assert remaining.value == 25 and remaining.currency == "USD"


def test_existing_report_without_confirmed_currency_is_refetched(warehouse):
    db, _ = warehouse
    captured = datetime(2026, 11, 18, 12)
    db.add(DataCoverage(store_id="101", dimension="campaign_sp", metric_date=TARGET,
        status="COMPLETE", row_count=1, completed_at=captured))
    db.add(RawSnapshot(source="lingxing_ads_sp_campaign_report", store_id="101",
        captured_at=captured, payload={"report_date": TARGET.isoformat(), "rows": [{"campaign_id": "200"}]}))
    db.add(DailyMetric(store_id="101", dimension="campaign_sp", metric_date=TARGET, entity_id="200",
        metric_name="spend", value=25, currency=None, source="lingxing_ads_sp"))
    db.commit()
    provider = Provider()
    assert run(warehouse, provider, reader=lambda sid: sid == "101")["refreshed"] == 4
    assert ("101", "sp", TARGET.isoformat()) in provider.calls
    assert db.scalar(select(DailyMetric.currency).where(
        DailyMetric.metric_date == TARGET, DailyMetric.metric_name == "spend")) == "USD"


def test_repair_cannot_collect_an_open_day_or_overlap_active_collector(warehouse):
    db, _ = warehouse
    provider = Provider()
    assert run(warehouse, provider, on=date(2026, 11, 18))["attempted"] == 0
    with database_job_lock(db.get_bind(), "hourly_ad_monitor"):
        assert run(warehouse, provider)["busy"] is True
    assert not provider.calls


def test_mcp_is_archived_independently_and_never_fills_conversions(warehouse, monkeypatch):
    db, _ = warehouse

    class MCP:
        def __init__(self, api, settings, *, db):
            self.pending = []

        def observe_campaign(self, sid, day, kind):
            row = {"campaign_id": "200", "spend": 120, "clicks": 30}
            self.pending.append((sid, row))
            return [row]

        def flush(self, db, *, raw_only):
            assert raw_only
            for sid, value in self.pending:
                db.add(RawSnapshot(source="lingxing_mcp_campaign_sp", store_id=sid, payload=value))
            db.commit()
            self.pending.clear()

    monkeypatch.setattr(repair, "mcp_credentials", lambda _: True)
    monkeypatch.setattr(repair, "CollectionSources", MCP)
    provider = Provider(empty=True)
    result = run(warehouse, provider, reader=lambda sid: sid == "101")
    assert result["mcp_observations"] == 1 and result["refreshed"] == 0
    assert db.scalar(select(RawSnapshot.source)) == "lingxing_mcp_campaign_sp"
    assert db.scalar(select(DailyMetric)) is None
    assert db.scalar(select(DataCoverage)) is None
