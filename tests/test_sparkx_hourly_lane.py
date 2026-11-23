"""Selected-store routing is independent across vendors and source changes."""
from datetime import datetime, timezone
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

    async def provider(_settings, store, binding, *, now):
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
        assert lane.sparkx_hourly_pass(db, settings, slot="second", now=CLOCK) == {}
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 0


def test_scheduler_collects_selected_sparkx_while_lingxing_inventory_is_unavailable(world, monkeypatch):
    settings, engine = world
    calls = []

    async def provider(_settings, store, binding, *, now):
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
