from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import config, operations
from jx_alert.analysis import evaluator
from jx_alert.lingxing import pipeline
from jx_alert.lingxing.client import LingxingClient
from jx_alert.reporting import excel
from jx_alert.storage import db as database, raw_archive
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store, SyncJob
from jx_alert.store_controls import get_store_controls, save_store_controls


@pytest.fixture
def setup(tmp_path, monkeypatch):
    scope = tmp_path / "scope.json"
    zones = tmp_path / "timezones.json"
    ids = ["5174", "5175", "5176"]
    scope.write_text(json.dumps({"store_ids": ids}), encoding="utf-8")
    zones.write_text(json.dumps({"stores": {sid: {"timezone": "America/Los_Angeles"} for sid in ids}}),
                     encoding="utf-8")
    settings = config.Settings(_env_file=None, collection_scope_file=str(scope), store_timezones_file=str(zones),
        store_controls_file=str(tmp_path / "controls.json"), app_dry_run=False)
    engine = create_engine("sqlite:///" + (tmp_path / "store.sqlite").as_posix())
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add_all(Store(sid=sid, name="Store " + sid, status=1) for sid in ids)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "init_db", lambda: None)
    yield settings, sessions
    engine.dispose()


def test_collection_scope_switch_and_idempotency(setup):
    settings, sessions = setup
    with sessions.begin() as db:
        with pytest.raises(operations.CollectionBlocked, match="STORE_OUT_OF_SCOPE"):
            operations.enqueue_collection(db, store_ids=["9999"])
        get = get_store_controls(settings, "5176")
        save_store_controls(settings, "5176", False, True, get["revision"])
        with pytest.raises(operations.CollectionBlocked, match="SKIPPED_COLLECTION_DISABLED"):
            operations.enqueue_collection(db, store_ids=["5176"])
        db.get(Store, "5175").status = 2
        with pytest.raises(operations.CollectionBlocked, match="AUTHORIZATION_FAILED"):
            operations.enqueue_collection(db, store_ids=["5175"])
        existing = operations.enqueue_collection(db, store_ids=["5174"], idempotency_key="mcp")
        again = operations.enqueue_collection(db, store_ids=["5174"], idempotency_key="mcp")
        assert again == {**existing, "reused": True}
        active = operations.enqueue_collection(db, store_ids=["5174"])
        assert active == again
        assert len(db.scalars(select(SyncJob)).all()) == 1


def test_selected_retry_keeps_original_date_and_skips_completed_store(setup, monkeypatch):
    _, sessions = setup
    clock = datetime(2026, 11, 15, 7, 59, 40, tzinfo=timezone.utc)
    planner = operations.store_report_plan
    monkeypatch.setattr(operations, "store_report_plan",
        lambda records, path, **kw: planner(records, path, now=clock, **kw))
    monkeypatch.setattr(operations, "_utcnow", lambda: clock.replace(tzinfo=None))
    monkeypatch.setattr(LingxingClient, "from_settings", lambda: SimpleNamespace(
        get_stores=lambda: {"data": [{"sid": sid, "name": "Source " + str(sid), "status": 1}
                                     for sid in (5174, 5175, 5176)]}))
    monkeypatch.setattr(evaluator, "evaluate_daily_anomalies", lambda *args: {})
    monkeypatch.setattr(excel, "export_daily_reports", lambda *args, **kwargs: [])
    monkeypatch.setattr(raw_archive, "archive_raw_snapshots", lambda *args, **kwargs: {})
    seen = []

    def fetch(client, db, report_dates, *, stores_response, **kwargs):
        [(sid, day)] = report_dates.items()
        assert [str(row["sid"]) for row in stores_response["data"]] == [sid]
        seen.append((sid, day))
        if sid == "5175" and seen.count((sid, day)) == 1:
            raise ConnectionError("temporary failure")
        return {"status": "SUCCEEDED"}

    monkeypatch.setattr(pipeline, "collect_daily_snapshot", fetch)
    with sessions.begin() as db:
        task = operations.enqueue_collection(db, store_ids=["5175", "5174"])
    assert operations.process_pending_collection()["status"] == "PENDING"
    with sessions() as db:
        state = db.get(SyncJob, task["job_id"]).metadata_json
        assert state["planned_store_dates"] == {"5174": "2026-11-13", "5175": "2026-11-13"}
        assert state["completed_store_dates"] == {"5174": "2026-11-13"}
        assert db.get(Store, "5176").name == "Store 5176"
    clock += timedelta(seconds=40)
    assert operations.process_pending_collection()["status"] == "IDLE"
    clock += timedelta(seconds=21)
    assert operations.process_pending_collection()["status"] == "SUCCEEDED"
    assert seen == [("5174", "2026-11-13"), ("5175", "2026-11-13"), ("5175", "2026-11-13")]
