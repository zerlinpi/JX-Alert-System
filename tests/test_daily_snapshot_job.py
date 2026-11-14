from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.config import Settings
from jx_alert.reporting.daily_job import run_daily_snapshot
from jx_alert.storage.db import Base
from jx_alert.storage.models import SyncJob


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    from jx_alert import config
    from jx_alert.storage import db as database
    from jx_alert.reporting import daily_snapshot
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, app_dry_run=False, excel_archive_enabled=True,
                        excel_archive_dir=str(tmp_path / "excel"))
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(database, "engine", engine)
    calls = []

    def export(db, output, day, **kwargs):
        calls.append(day.isoformat())
        return {"status": "COMPLETE" if len(calls) == 1 else "ALREADY_COMPLETE",
                "snapshot_date": day.isoformat(), "manifest_path": str(tmp_path / "manifest.json"),
                "files": ["business.xlsx"], "datasets": [], "row_count": 12}
    monkeypatch.setattr(daily_snapshot, "export_daily_snapshot", export)
    yield settings, sessions, calls, daily_snapshot, export
    engine.dispose()


def test_default_schedule_and_snapshot_use_beijing_even_if_application_timezone_differs(warehouse):
    settings, sessions, calls, _, _ = warehouse
    settings.app_timezone = "UTC"
    assert settings.daily_sync_cron == "30 18 * * *"
    assert run_daily_snapshot(now=datetime(2026, 11, 14, 15, 29, tzinfo=timezone.utc))["status"] == "WAITING"
    assert calls == []
    result = run_daily_snapshot(now=datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc))
    assert result["snapshot_date"] == "2026-11-14" and calls == ["2026-11-14"]
    with sessions() as db:
        row = db.scalar(select(SyncJob).where(SyncJob.job_type == "excel_daily_snapshot"))
        assert row.status == "SUCCEEDED" and row.record_count == 12


def test_manual_export_before_deadline_then_scheduled_recovery_reuses_same_job(warehouse):
    _, sessions, calls, _, _ = warehouse
    clock = datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc)
    first = run_daily_snapshot(snapshot_date="2026-11-14", now=clock)
    again = run_daily_snapshot(now=clock.replace(hour=15, minute=30))
    assert again["status"] == "ALREADY_COMPLETE" and first["job_id"] == again["job_id"]
    with sessions() as db:
        assert len(db.scalars(select(SyncJob)).all()) == 1
    assert calls == ["2026-11-14", "2026-11-14"]


def test_disk_failure_survives_midnight_and_retries_existing_date_without_provider(warehouse, monkeypatch):
    _, sessions, calls, module, successful = warehouse
    def disk_failure(*args, **kwargs):
        raise OSError("private-path-detail-must-not-be-logged")
    monkeypatch.setattr(module, "export_daily_snapshot", disk_failure)
    failed = run_daily_snapshot(now=datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc))
    assert failed["status"] == "RETRY_PENDING" and failed["error_type"] == "OSError"
    assert "private-path" not in json.dumps(failed)
    monkeypatch.setattr(module, "export_daily_snapshot", successful)
    retried = run_daily_snapshot(now=datetime(2026, 11, 14, 16, 1, tzinfo=timezone.utc))
    assert retried["snapshot_date"] == "2026-11-14" and retried["job_id"] == failed["job_id"]
    assert calls == ["2026-11-14"]
    with sessions() as db:
        row = db.get(SyncJob, failed["job_id"])
        assert row.status == "SUCCEEDED" and row.metadata_json["attempts"] == 2 and row.error_message is None


def test_disabled_dry_run_future_date_and_naive_clock_do_not_export(warehouse):
    settings, _, calls, _, _ = warehouse
    clock = datetime(2026, 11, 14, 16, tzinfo=timezone.utc)
    settings.excel_daily_snapshot_enabled = False
    assert run_daily_snapshot(now=clock)["status"] == "DISABLED"
    settings.excel_daily_snapshot_enabled = True
    settings.app_dry_run = True
    assert run_daily_snapshot(now=clock)["status"] == "DRY_RUN"
    settings.app_dry_run = False
    with pytest.raises(ValueError, match="existing Beijing"):
        run_daily_snapshot(snapshot_date="2026-11-16", now=clock)
    with pytest.raises(ValueError, match="aware"):
        run_daily_snapshot(now=clock.replace(tzinfo=None))
    assert calls == []


def test_persistently_failing_old_date_does_not_block_todays_due_snapshot(warehouse, monkeypatch):
    _, sessions, calls, module, successful = warehouse
    def only_old_directory_broken(db, output, day, **kwargs):
        if day.isoformat() == "2026-11-14":
            raise PermissionError("old-directory")
        return successful(db, output, day, **kwargs)
    monkeypatch.setattr(module, "export_daily_snapshot", only_old_directory_broken)
    old = run_daily_snapshot(now=datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc))
    assert old["status"] == "RETRY_PENDING"
    today = run_daily_snapshot(now=datetime(2026, 11, 15, 15, 30, tzinfo=timezone.utc))
    assert today["status"] == "COMPLETE" and today["snapshot_date"] == "2026-11-15"
    assert calls == ["2026-11-15"]
    recovered = run_daily_snapshot(now=datetime(2026, 11, 15, 15, 35, tzinfo=timezone.utc))
    assert recovered["status"] == "RETRY_PENDING" and recovered["job_id"] == old["job_id"]
    with sessions() as db:
        assert db.get(SyncJob, today["job_id"]).status == "SUCCEEDED"
