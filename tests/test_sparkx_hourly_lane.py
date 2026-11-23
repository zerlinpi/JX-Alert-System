"""Selected-store routing is independent across vendors and source changes."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from jx_alert import hourly_ad_monitor as monitor, sparkx_hourly_lane as lane
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.sparkx_backend import SOURCES
from jx_alert.storage.db import Base
from jx_alert.storage.models import RawSnapshot, Store, SyncJob


CLOCK = datetime(2026, 11, 24, 18, tzinfo=timezone.utc)
STAMP = "2024-01-01T00:00:00+00:00"
BINDING = dict(profile_id=7001, marketplace="ATVPDKIKX0DER", currency="USD", timezone="America/Los_Angeles")


def _choice(enabled=True, valid=True):
    return {"source": "sparkx" if enabled else "lingxing", "binding": BINDING, "updated_at": STAMP,
            "selected_at": STAMP, "verification": {"verified_at": STAMP, "token_fingerprint":
                hashlib.sha256(("jx-sparkx-observation-verifier:v1\0" + ("faux" if valid else "old")).encode()).hexdigest()}}


def _save_selection(settings, choice):
    Path(settings.advertising_source_controls_file).write_text(json.dumps({"version": 1, "stores": {"5174": choice}}))


@pytest.fixture
def world(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "stores.db"))
    Base.metadata.create_all(engine)
    from jx_alert.config import Settings
    values = {
        "collection_scope_file": {"store_ids": ["5174", "5175"]},
        "store_timezones_file": {"stores": {sid: {"timezone": BINDING["timezone"], "currency_code": "USD"}
                                          for sid in ("5174", "5175")}},
    }
    kwargs = {}
    for key, value in values.items():
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(value))
        kwargs[key] = str(path)
    settings = Settings(_env_file=None, app_dry_run=False, scheduler_enabled=True, hourly_ad_monitor_enabled=True,
        sparkx_ai_token="faux", sparkx_profile_ids="7001", raw_dir=str(tmp_path / "raw"),
        store_controls_file=str(tmp_path / "pauses.json"),
        advertising_source_controls_file=str(tmp_path / "sources.json"), **kwargs)
    _save_selection(settings, _choice())
    with Session(engine) as db:
        for sid in ("5174", "5175"):
            db.add(Store(sid=sid, name=f"Store {sid}", status=1, country="US", timezone=BINDING["timezone"],
                         currency_code="USD", marketplace_id=BINDING["marketplace"], has_ads_setting=1))
        db.commit()
    yield settings, engine
    engine.dispose()


def _reply():
    return {"daily": {"granularity": "daily", "row_count": 1, "report_date": "2026-11-23",
                      "rows": [{"campaign_id": "3"}], "query_started_at_utc": "2026-11-24T18:00:00+00:00",
                      "provider_watermark": None},
            "hourly": {"granularity": "hourly", "row_count": 1, "report_date": "2026-11-24", "rows": [],
                       "query_started_at_utc": "2026-11-24T18:00:00+00:00", "provider_watermark": None}}


def test_manual_switch_discards_inflight_and_does_not_fall_back(world, monkeypatch):
    settings, engine = world
    seen = []

    async def provider(_settings, store, binding, *, now, authorize):
        seen.append((store.sid, binding["profile_id"]))
        _save_selection(settings, _choice(enabled=False))
        return _reply(), {}

    monkeypatch.setattr(lane, "read_sparkx_observations", provider)
    with Session(engine) as db:
        result = lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK)
        assert result["5174"]["reason"] == "SCOPE_CHANGED"
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 0
        assert lane.selected_ad_provider(settings, "5174") == "lingxing"
        assert lane.sparkx_hourly_pass(db, settings, slot="second", now=CLOCK) == {}
    assert seen == [("5174", 7001)]


def test_unverified_selection_and_collection_pause_never_call_vendor(world, monkeypatch):
    settings, engine = world
    monkeypatch.setattr(lane, "read_sparkx_observations", lambda *_a, **_kw: pytest.fail("external read attempted"))
    _save_selection(settings, _choice(valid=False))
    with Session(engine) as db:
        blocked = lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK)
        assert blocked["5174"]["status"] == "BLOCKED"
        assert lane.selected_ad_provider(settings, "5174") == "sparkx"
        _save_selection(settings, _choice())
        Path(settings.store_controls_file).write_text(json.dumps({"version": 1, "stores": {"5174": {
            "collection_enabled": False, "notifications_enabled": True, "updated_at": STAMP}}}))
        paused = lane.sparkx_hourly_pass(db, settings, slot="second", now=CLOCK)
        assert paused["5174"]["status"] == "PAUSED"
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 0


def test_scheduler_collects_selected_sparkx_while_lingxing_inventory_is_unavailable(world, monkeypatch):
    settings, engine = world
    calls = []

    async def provider(_settings, store, binding, *, now, authorize):
        calls.append(store.sid)
        return _reply(), {}

    monkeypatch.setattr(lane, "read_sparkx_observations", provider)

    class Down:
        def get_stores(self):
            raise RuntimeError("test-only Lingxing availability error")

    result = monitor.run_hourly_monitor(now=CLOCK, settings=settings, warehouse_engine=engine,
        session_factory=sessionmaker(engine), client_factory=Down, send_alerts=False)
    assert result["status"] == "PARTIAL_FAILED" and calls == ["5174"]
    with Session(engine) as db:
        job = db.get(SyncJob, result["job_id"])
        assert job.metadata_json["error_phase"] == "inventory"
        assert job.metadata_json["sparkx_observations"]["5174"]["status"] == "OBSERVED"
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        raw = db.scalars(select(RawSnapshot).order_by(RawSnapshot.id)).all()
        assert [(row.source, row.store_id) for row in raw] == [(SOURCES["daily"], "5174"), (SOURCES["hourly"], "5174")]
        assert all(row.payload["provider_watermark"] is None for row in raw)


def test_selected_or_unverified_store_does_not_enter_lingxing_ad_collection(world, monkeypatch):
    from jx_alert.lingxing import campaign_collector
    settings, engine = world
    monkeypatch.setattr(campaign_collector, "collect_campaigns", lambda *_a, **_kw: pytest.fail("wrong provider"))
    _save_selection(settings, _choice(valid=False))
    with Session(engine) as db:
        result = monitor._collect_store(object(), db, settings,
            {"sid": "5174", "has_ads_setting": 1}, CLOCK.date(), now=CLOCK)
        assert not result["ad_ready"] and not result["intraday_ready"]
        assert result["ads"] == "NON_LINGXING_SELECTION"


def test_replaced_sparkx_generation_before_dispatch_is_never_read(world, monkeypatch):
    settings, engine = world

    class DelayedWorker:
        def __init__(self, **_options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def map(self, call, work):
            # A different revision still points to SparkX. A source-name-only
            # recheck would call the old verified binding anyway.
            second = "2024-01-01T00:00:01+00:00"
            _save_selection(settings, {**_choice(), "selected_at": second, "updated_at": second})
            return map(call, work)

    monkeypatch.setattr(lane, "ThreadPoolExecutor", DelayedWorker)
    monkeypatch.setattr(lane, "read_sparkx_observations", lambda *_a, **_kw: pytest.fail("unapproved read"))
    with Session(engine) as db:
        result = lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK)
        assert result["5174"]["reason"] == "SCOPE_CHANGED"
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 0


def test_console_switch_serializes_against_scope_checked_snapshot_commit(world, monkeypatch):
    from jx_alert import ad_source_controls as controls

    settings, engine = world
    persist = lane.persist_sparkx_observation
    rejected = []

    async def provider(_settings, store, binding, *, now, authorize):
        authorize()
        return _reply(), {}

    def competing_switch(db, store, selection, observation, **kwargs):
        with pytest.raises(controls.AdSourceError) as error:
            controls.save_selection(settings, store, {"revision": selection["revision"], "source": "lingxing"},
                                    authorize=lambda: None)
        rejected.append(error.value.status_code)
        return persist(db, store, selection, observation, **kwargs)

    monkeypatch.setattr(lane, "read_sparkx_observations", provider)
    monkeypatch.setattr(lane, "persist_sparkx_observation", competing_switch)
    with Session(engine) as db:
        observed = lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK)
        assert rejected == [409, 409] and observed["5174"]["status"] == "OBSERVED"
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 2
        assert lane.selected_ad_provider(settings, "5174") == "sparkx"

        monkeypatch.setattr(lane, "read_sparkx_observations", lambda *_a, **_kw: pytest.fail("repeat read"))
        replay = lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK)
        assert replay == observed
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 2

        current = controls.source_snapshot(settings, "5174")
        switched = controls.save_selection(settings, db.get(Store, "5174"),
            {"revision": current["revision"], "source": "lingxing"}, authorize=lambda: None)
        assert switched["source"] == "lingxing"
        assert lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK) == {}


@pytest.mark.parametrize("paused", [False, True])
def test_hybrid_hourly_job_distinguishes_incomplete_sparkx_from_intentional_pause(world, monkeypatch, paused):
    settings, engine = world
    _save_selection(settings, _choice(valid=False))
    if paused:
        Path(settings.store_controls_file).write_text(json.dumps({"version": 1, "stores": {"5174": {
            "collection_enabled": False, "notifications_enabled": True, "updated_at": STAMP}}}))
    inspected, analysed = [], []

    class Lingxing:
        def get_stores(self):
            return {"data": [{"sid": sid, "name": "Fixture Shop " + sid, "status": 1, "country": "US",
                              "marketplace_id": BINDING["marketplace"], "has_ads_setting": 1}
                             for sid in ("5174", "5175")]}

    def collect(client, db, config, record, target, *, lingxing_ads=True, **_kwargs):
        inspected.append((record["sid"], lingxing_ads))
        assert (record["sid"] == "5175") == lingxing_ads
        return {"status": "COMPLETE", "ad_ready": lingxing_ads}

    def review(db, config, stores, dates, **_kwargs):
        analysed.append({store.sid for store in stores})
        return {"campaigns": []}

    monkeypatch.setattr(monitor, "_collect_store", collect)
    monkeypatch.setattr(monitor, "build_ad_review_context", review)
    monkeypatch.setattr(lane, "read_sparkx_observations", lambda *_a, **_kw: pytest.fail("unapproved read"))

    def run(at):
        return monitor.run_hourly_monitor(now=at, settings=settings, warehouse_engine=engine,
            session_factory=sessionmaker(engine), client_factory=Lingxing, send_alerts=False)

    first = run(CLOCK)
    assert first["status"] == ("SUCCEEDED" if paused else "PARTIAL_FAILED")
    assert analysed and all(result == {"5175"} for result in analysed)
    assert set(inspected) == ({("5175", True)} if paused else {("5174", False), ("5175", True)})
    assert run(CLOCK + timedelta(minutes=1))["status"] == ("ALREADY_COMPLETE" if paused else "WAITING")
    if not paused:
        assert run(CLOCK + timedelta(minutes=5))["status"] == "PARTIAL_FAILED"
        assert run(CLOCK + timedelta(minutes=10))["status"] == "PARTIAL_FAILED"
        assert run(CLOCK + timedelta(minutes=15))["status"] == "RETRY_LIMIT"
    with Session(engine) as db:
        job = db.get(SyncJob, first["job_id"])
        assert job.metadata_json["sparkx_observations"]["5174"]["status"] == ("PAUSED" if paused else "BLOCKED")
        assert job.metadata_json["attempts"] == (1 if paused else 3)
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_selected_source_without_explicit_scope_is_blocked_not_omitted(world, monkeypatch):
    settings, engine = world
    Path(settings.collection_scope_file).unlink()
    monkeypatch.setattr(lane, "read_sparkx_observations", lambda *_a, **_kw: pytest.fail("unapproved read"))
    with Session(engine) as db:
        outcome = lane.sparkx_hourly_pass(db, settings, slot="first", now=CLOCK)
        assert outcome["5174"]["status"] == "BLOCKED"
        assert outcome["5174"]["reason"] == "EXPLICIT_COLLECTION_SCOPE_REQUIRED"
