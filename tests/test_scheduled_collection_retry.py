from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import operations, scheduler
from jx_alert.storage import db as database
from jx_alert.storage.db import Base
from jx_alert.storage.models import SyncJob


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "scheduled.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(scheduler, "get_settings", lambda: SimpleNamespace(app_dry_run=False))
    yield sessions
    engine.dispose()


def test_daily_plan_is_once_per_beijing_day_and_never_collects_in_cron(warehouse, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("The cron callback must persist work without running provider requests")

    monkeypatch.setattr(operations, "run_collection", forbidden)
    before_midnight = datetime(2026, 11, 13, 15, 59, tzinfo=timezone.utc)
    after_midnight = before_midnight + timedelta(minutes=2)
    scheduler.daily_job(now=before_midnight)
    scheduler.daily_job(now=before_midnight + timedelta(seconds=30))
    scheduler.daily_job(now=after_midnight)
    scheduler.daily_job(now=after_midnight + timedelta(hours=1))
    with warehouse() as db:
        jobs = db.scalars(select(SyncJob).order_by(SyncJob.id)).all()
        assert len(jobs) == 2
        assert [row.metadata_json["idempotency_key"] for row in jobs] == [
            "scheduled-collection:2026-11-13", "scheduled-collection:2026-11-14"]
        assert all(row.job_type == "manual_collection" and row.status == "PENDING" for row in jobs)
        assert all(row.metadata_json["report_date"] is None for row in jobs)
        assert all(row.metadata_json["backfill_days"] == 2 for row in jobs)


def test_daily_retry_preserves_checkpoint_and_repeated_cron_does_not_reset_it(warehouse, monkeypatch):
    clock = datetime(2026, 11, 14, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(operations, "_utcnow", lambda: clock.replace(tzinfo=None))
    scheduler.daily_job(now=clock)
    completed = {"5174": "2026-11-13"}

    def interrupted(**kwargs):
        assert kwargs["backfill_days"] == 2 and kwargs["report_date"] is None
        kwargs["progress"]({"completed_store_dates": completed,
                            "store_statuses": {"5174": "COMPLETE"}})
        raise RuntimeError("provider failed after a completed store")

    monkeypatch.setattr(operations, "run_collection", interrupted)
    first = operations.process_pending_collection()
    assert first["status"] == "PENDING"
    with warehouse() as db:
        row = db.get(SyncJob, first["job_id"])
        expected = dict(row.metadata_json)
        assert expected["completed_store_dates"] == completed
        assert expected["attempts"] == 1 and expected["retry_at"]
    scheduler.daily_job(now=clock + timedelta(minutes=1))
    with warehouse() as db:
        rows = db.scalars(select(SyncJob)).all()
        assert len(rows) == 1 and rows[0].metadata_json == expected

    clock += timedelta(minutes=2)
    def resumed(**kwargs):
        assert kwargs["completed_store_dates"] == completed
        return {"status": "COMPLETE", "completed_store_ids": ["5174"],
                "completed_store_dates": completed}

    monkeypatch.setattr(operations, "run_collection", resumed)
    assert operations.process_pending_collection() == {"job_id": first["job_id"], "status": "SUCCEEDED"}
    scheduler.daily_job(now=clock)
    with warehouse() as db:
        rows = db.scalars(select(SyncJob)).all()
        assert len(rows) == 1 and rows[0].status == "SUCCEEDED"
        assert rows[0].metadata_json["completed_store_dates"] == completed
        assert "retry_at" not in rows[0].metadata_json


def test_terminal_daily_failure_is_not_rearmed_by_duplicate_cron(warehouse, monkeypatch):
    clock = datetime(2026, 11, 14, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(operations, "_utcnow", lambda: clock.replace(tzinfo=None))
    scheduler.daily_job(now=clock)
    calls = []

    def failed(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("provider still unavailable")

    monkeypatch.setattr(operations, "run_collection", failed)
    for _ in range(3):
        result = operations.process_pending_collection()
        clock += timedelta(minutes=5)
    assert result["status"] == "FAILED" and len(calls) == 3
    scheduler.daily_job(now=clock)
    assert operations.process_pending_collection()["status"] == "IDLE"
    with warehouse() as db:
        jobs = db.scalars(select(SyncJob)).all()
        assert len(jobs) == 1 and jobs[0].status == "FAILED"
        assert jobs[0].metadata_json["attempts"] == 3


def test_dry_run_and_naive_clock_never_persist_a_daily_plan(warehouse, monkeypatch):
    scheduler.daily_job(now=datetime(2026, 11, 14, 10, 30))
    monkeypatch.setattr(scheduler, "get_settings", lambda: SimpleNamespace(app_dry_run=True))
    scheduler.daily_job(now=datetime(2026, 11, 14, 10, 30, tzinfo=timezone.utc))
    with warehouse() as db:
        assert db.scalar(select(SyncJob)) is None
