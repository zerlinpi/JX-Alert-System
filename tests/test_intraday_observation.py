from datetime import datetime, timedelta, timezone
from contextlib import closing
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.config import Settings
from jx_alert.lingxing import intraday_observation as observation
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot, Store, SyncJob


NOW = datetime(2026, 11, 14, 20, tzinfo=timezone.utc)
DAY = "2026-11-14"


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    from jx_alert.storage import db as database
    from jx_alert import config
    zones, scope = tmp_path / "zones.json", tmp_path / "scope.json"
    zones.write_text(json.dumps({"stores": {"5174": {"timezone": "America/Los_Angeles"}}}))
    scope.write_text(json.dumps({"store_ids": [5174]}))
    settings = Settings(_env_file=None, app_dry_run=False, scheduler_enabled=True, mcp_operations_enabled=True,
        collection_scope_file=str(scope), store_timezones_file=str(zones), store_controls_file=str(tmp_path / "controls.json"),
        raw_dir=str(tmp_path / "raw"), excel_archive_enabled=True, excel_archive_dir=str(tmp_path / "excel"))
    engine = create_engine("sqlite:///" + str(tmp_path / "db.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    with sessions.begin() as db:
        db.add(Store(sid="5174", name="Geteen-US", timezone="America/Los_Angeles", status=1, has_ads_setting=1))
        db.add(DailyMetric(store_id="5174", dimension="campaign_sp", entity_id="a", metric_date=datetime.fromisoformat(DAY).date(), metric_name="spend", value=55, currency="USD"))
        db.add(DataCoverage(store_id="5174", dimension="campaign_sp", metric_date=datetime.fromisoformat(DAY).date(), status="COMPLETE", row_count=1))
    yield settings, sessions
    engine.dispose()


class Client:
    def get_stores(self):
        return {"data": [{"sid": 5174, "status": 1, "has_ads_setting": 1}]}

    def seller_ad_accounts(self):
        return [{"sid": 5174, "profile_id": 17, "type": "seller", "currency_code": "USD"}]

    def iter_advertising_report(self, kind, payload, **kwargs):
        assert kind == "sp" and payload["report_date"] == DAY
        yield {"campaign_id": 9, "profile_id": 17, "report_date": DAY,
               "campaign_name": "=unsafe formula", "cost": 5.25, "orders": 0, "clicks": 3, "impressions": 100}


def args():
    return {"store_ids": ["5174"], "observation_date": DAY}


def test_raw_and_excel_observations_never_overwrite_daily_facts_or_enqueue_notifications(warehouse):
    from openpyxl import load_workbook
    settings, sessions = warehouse
    with sessions() as db:
        result = observation.run_observation(db, settings, args(), client=Client(), now=NOW)
        assert result["status"] == "COMPLETE" and result["row_count"] == 1
        assert db.scalar(select(DailyMetric.value)) == 55
        assert db.scalar(select(DataCoverage.status)) == "COMPLETE"
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        row = db.get(RawSnapshot, result["snapshot_id"])
        assert row.source == observation.SOURCE and row.payload["observation_only"]
        assert row.payload["rows"][0]["confirmed_currency"] == "USD"
        assert observation.latest_observation(db, "5174", DAY).id == row.id
        assert result["raw_archive"]["written"] == 1
        with closing(load_workbook(result["excel_file"], read_only=True, data_only=False)) as book:
            values = list(book["SP盘中观测"].values)
            assert len(values) == 2 and values[1][4] == "=unsafe formula"
            assert book["SP盘中观测"]["E2"].data_type == "s"


def test_empty_response_is_a_saved_empty_observation_not_zero_spend(warehouse):
    settings, sessions = warehouse
    class Empty(Client):
        def iter_advertising_report(self, *args, **kwargs):
            return iter([])
    with sessions() as db:
        result = observation.run_observation(db, settings, args(), client=Empty(), now=NOW)
        assert result["availability"] == "EMPTY" and result["row_count"] == 0
        assert db.scalar(select(DailyMetric.value)) == 55


def test_same_private_store_and_day_reuses_pending_job_but_finished_job_can_be_refreshed(warehouse):
    settings, sessions = warehouse
    with sessions() as db:
        first = observation.enqueue_observation(db, settings, "5174", now=NOW)
        again = observation.enqueue_observation(db, settings, "5174", now=NOW)
        assert first["job_id"] == again["job_id"] and again["reused"]
        db.get(SyncJob, first["job_id"]).status = "SUCCEEDED"
        db.flush()
        assert observation.enqueue_observation(db, settings, "5174", now=NOW)["job_id"] != first["job_id"]


@pytest.mark.parametrize("bad", ["wrong_day", "wrong_store", "missing_campaign", "negative", "nan"])
def test_bad_observation_does_not_create_raw_or_change_daily_data(warehouse, bad):
    settings, sessions = warehouse
    class Bad(Client):
        def iter_advertising_report(self, *args, **kwargs):
            row = next(Client().iter_advertising_report("sp", {"report_date": DAY}))
            if bad == "wrong_day":
                row["report_date"] = "2026-11-13"
            elif bad == "wrong_store":
                row["sid"] = 999
            elif bad == "missing_campaign":
                row.pop("campaign_id")
            else:
                row["cost"] = -1 if bad == "negative" else float("nan")
            return iter([row])
    with sessions() as db, pytest.raises(ValueError):
        observation.run_observation(db, settings, args(), client=Bad(), now=NOW)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 0
        assert db.scalar(select(DailyMetric.value)) == 55


def test_private_observation_rechecks_pause_before_provider_and_keeps_daily_date_guard(warehouse):
    from jx_alert.operations import CollectionBlocked, _completed_dates
    from jx_alert.store_controls import get_store_controls, save_store_controls
    settings, sessions = warehouse
    save_store_controls(settings, "5174", False, True, get_store_controls(settings, "5174")["revision"])
    with sessions() as db, pytest.raises(CollectionBlocked):
        observation.run_observation(db, settings, args(), client=object(), now=NOW)
    with pytest.raises(ValueError, match="Only completed"):
        _completed_dates(DAY, {"5174": "2026-11-13"})


def test_observation_dispatch_uses_retry_backoff_and_never_calls_daily_pipeline(warehouse, monkeypatch):
    from jx_alert import operations
    settings, sessions = warehouse
    clock = NOW.replace(tzinfo=None)
    monkeypatch.setattr(operations, "_utcnow", lambda: clock)
    with sessions() as db:
        job_id = observation.enqueue_observation(db, settings, "5174", now=NOW)["job_id"]
        db.commit()
    calls = []
    def collect(db, settings, metadata):
        calls.append(metadata)
        if len(calls) == 1:
            raise TimeoutError("private provider detail")
        return {"status": "COMPLETE", "observation_only": True, "row_count": 1}
    monkeypatch.setattr(observation, "run_observation", collect)
    monkeypatch.setattr(operations, "run_collection", lambda **kw: pytest.fail("Intraday cannot enter daily analysis"))
    assert operations.process_pending_collection() == {"job_id": job_id, "status": "PENDING"}
    assert operations.process_pending_collection()["status"] == "IDLE"
    clock += timedelta(minutes=1)
    assert operations.process_pending_collection() == {"job_id": job_id, "status": "SUCCEEDED"}
    with sessions() as db:
        job = db.get(SyncJob, job_id)
        assert job.metadata_json["attempts"] == 1 and "private provider detail" not in json.dumps(job.metadata_json)
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_unconfirmed_currency_is_not_invented_from_store_region(warehouse):
    settings, sessions = warehouse
    class MissingCurrency(Client):
        def seller_ad_accounts(self): return []
    with sessions() as db:
        result = observation.run_observation(db, settings, args(), client=MissingCurrency(), now=NOW)
        assert db.get(RawSnapshot, result["snapshot_id"]).payload["rows"][0]["confirmed_currency"] is None


def test_later_failed_request_does_not_present_an_old_observation_as_new(warehouse):
    settings, sessions = warehouse
    with sessions() as db:
        observation.run_observation(db, settings, args(), client=Client(), now=NOW)
        queued = observation.enqueue_observation(db, settings, "5174", now=NOW + timedelta(minutes=5))
        db.get(SyncJob, queued["job_id"]).status = "FAILED"
        db.commit()
        assert observation.latest_observation(db, "5174", DAY) is None
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 1


def test_actual_dual_source_fallback_flushes_only_raw_observations(warehouse):
    from jx_alert.lingxing.source_gateway import CollectionSources
    settings, sessions = warehouse
    class Reader:
        def stores(self):
            return [{"id": 5174}], {"code": 1, "list": [{"id": 5174}]}
        def profiles(self):
            return [{"sid": 5174, "profile_id": 17, "type": "seller", "real_status": 1}], {"code": 1}
        def campaign_ads(self, sid, day, kind):
            return [{"entity_id": "9", "profile_id": "17", "name": "MCP campaign",
                     "spend": 5.25, "clicks": 3, "impressions": 100}], {"code": 1, "data": {"list": []}}
    with sessions() as db:
        db.add(RawSnapshot(source="lingxing_seller_ad_accounts", payload={"rows": Client().seller_ad_accounts()}))
        db.commit()
        gateway = CollectionSources(None, settings, db=db, reader=Reader(), observation_mode=True)
        result = observation.run_observation(db, settings, args(), client=gateway, now=NOW)
        snapshot = db.get(RawSnapshot, result["snapshot_id"])
        assert snapshot.payload["sources"] == ["official_mcp_observation"]
        row = snapshot.payload["rows"][0]
        assert row["cost"] == 5.25 and row["confirmed_currency"] == "USD"
        assert row.get("orders") is None and row.get("sales") is None
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 1
        assert db.scalar(select(func.count()).select_from(DataCoverage)) == 1
        assert db.scalar(select(DailyMetric.value)) == 55
        assert db.scalar(select(DataCoverage.status)) == "COMPLETE"
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        sources = set(db.scalars(select(RawSnapshot.source)))
        assert {"lingxing_mcp_campaign_sp", "lingxing_source_selection", observation.SOURCE} <= sources
