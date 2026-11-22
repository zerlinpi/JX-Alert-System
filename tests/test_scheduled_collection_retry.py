from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import hourly_ad_monitor, operations, scheduler
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
    settings = SimpleNamespace(app_dry_run=False, daily_sync_cron="30 18 * * *")
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    yield sessions
    engine.dispose()


@pytest.mark.parametrize("hourly_mode", [False, True])
def test_daily_plan_is_once_per_beijing_day_and_never_collects_in_cron(warehouse, monkeypatch, hourly_mode):
    scheduler.get_settings().hourly_ad_monitor_enabled = hourly_mode
    def forbidden(**kwargs):
        pytest.fail("The cron callback must persist work without running provider requests")

    monkeypatch.setattr(operations, "run_collection", forbidden)
    monkeypatch.setattr(hourly_ad_monitor, "run_hourly_monitor", forbidden)
    first_due = datetime(2026, 11, 13, 10, 30, tzinfo=timezone.utc)
    second_due = first_due + timedelta(days=1)
    scheduler.daily_job(now=first_due)
    scheduler.daily_job(now=first_due + timedelta(seconds=30))
    scheduler.daily_job(now=second_due)
    scheduler.daily_job(now=second_due + timedelta(minutes=1))
    with warehouse() as db:
        jobs = db.scalars(select(SyncJob).order_by(SyncJob.id)).all()
        assert len(jobs) == 2
        assert [row.metadata_json["idempotency_key"] for row in jobs] == [
            "scheduled-collection:2026-11-13", "scheduled-collection:2026-11-14"]
        assert all(row.job_type == "manual_collection" and row.status == "PENDING" for row in jobs)
        assert all(row.metadata_json["report_date"] is None for row in jobs)
        assert all(row.metadata_json["backfill_days"] == 2 for row in jobs)


@pytest.mark.parametrize("startup_enqueue_fails", [False, True])
@pytest.mark.parametrize("hourly_mode", [False, True])
def test_startup_and_periodic_recovery_restore_missed_collection_once(
    warehouse, monkeypatch, startup_enqueue_fails, hourly_mode,
):
    settings = scheduler.get_settings()
    settings.scheduler_enabled = True
    settings.app_timezone = "UTC"
    settings.hourly_ad_monitor_enabled = hourly_mode
    settings.hourly_ad_monitor_cron = "0 * * * *"
    settings.feishu_daily_report_cron = "0 8 * * *"
    settings.excel_archive_enabled = True
    settings.excel_daily_snapshot_cron = "30 23 * * *"
    jobs = {}

    class FakeScheduler:
        def __init__(self, **kwargs):
            pass

        def add_job(self, callback, trigger, *, id, **kwargs):
            jobs[id] = (callback, trigger)

        def start(self):
            pass

    due = datetime(2026, 11, 14, 10, 30, tzinfo=timezone.utc)
    clock = due - timedelta(minutes=1)
    daily_job = scheduler.daily_job
    monkeypatch.setattr(scheduler, "daily_job", lambda: daily_job(now=clock))
    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    monkeypatch.setattr(scheduler, "daily_report_job", lambda: None)
    monkeypatch.setattr(scheduler, "daily_excel_snapshot_job", lambda: None)
    enqueue = operations.enqueue_collection
    if startup_enqueue_fails:
        def transient_failure(*args, **kwargs):
            if clock == due + timedelta(minutes=10):
                raise RuntimeError("temporary database failure")
            return enqueue(*args, **kwargs)

        monkeypatch.setattr(operations, "enqueue_collection", transient_failure)

    def job_keys():
        with warehouse() as db:
            return [row.metadata_json["idempotency_key"] for row in db.scalars(
                select(SyncJob).order_by(SyncJob.id),
            )]

    scheduler.main()  # Stop at 18:29 without allowing the cron callback to fire.
    assert job_keys() == []
    clock = due + timedelta(minutes=10)
    scheduler.main()
    expected = ["scheduled-collection:2026-11-14"]
    assert job_keys() == ([] if startup_enqueue_fails else expected)
    clock += timedelta(minutes=1)
    recovery_callback, recovery_trigger = jobs["daily_pipeline_recovery"]
    assert recovery_trigger.interval == timedelta(minutes=1)
    recovery_callback()
    scheduler.main()
    assert job_keys() == expected

    assert jobs["daily_pipeline"][1].get_next_fire_time(None, due) == due
    for job_id, deadline in (
        ("feishu_daily_report", due.replace(hour=0, minute=0)),
        ("excel_daily_snapshot", due.replace(hour=15, minute=30)),
    ):
        assert jobs[job_id][1].get_next_fire_time(None, deadline) == deadline


def test_recovery_window_does_not_enqueue_after_grace_or_across_midnight(warehouse):
    due = datetime(2026, 11, 14, 10, 30, tzinfo=timezone.utc)
    scheduler.daily_job(now=due + timedelta(minutes=30, seconds=1))
    settings = scheduler.get_settings()
    settings.daily_sync_cron = "50 23 * * *"
    scheduler.daily_job(now=due.replace(hour=16, minute=5))  # Beijing 00:05 next day.
    with warehouse() as db:
        assert db.scalar(select(SyncJob)) is None


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


@pytest.mark.parametrize("broad_cron,broad_minute", [(None, 30), ("45 18 * * *", 45)])
def test_hourly_mode_keeps_independent_broad_hourly_and_delivery_schedules(warehouse, monkeypatch, broad_cron, broad_minute):
    settings = scheduler.get_settings()
    settings.scheduler_enabled = True
    settings.hourly_ad_monitor_enabled = True
    settings.app_timezone = "UTC"
    settings.daily_sync_cron = "0 8 * * *"
    if broad_cron is not None:
        settings.hourly_mode_daily_sync_cron = broad_cron
    settings.hourly_ad_monitor_cron = "0 * * * *"
    settings.feishu_daily_report_cron = "0 9 * * *"
    jobs = {}

    class FakeScheduler:
        def __init__(self, **kwargs):
            pass

        def add_job(self, callback, trigger, *, id, **kwargs):
            jobs[id] = (callback, trigger, kwargs)

        def start(self):
            pass

    due = datetime(2026, 11, 19, tzinfo=timezone.utc)  # 08:00 Beijing
    broad_due = due.replace(hour=10, minute=broad_minute)
    daily_job = scheduler.daily_job
    clock = due
    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    monkeypatch.setattr(scheduler, "daily_job", lambda: daily_job(now=clock))
    monkeypatch.setattr(scheduler, "daily_report_job", lambda: None)
    monkeypatch.setattr(scheduler, "daily_excel_snapshot_job", lambda: None)
    scheduler.main()

    # Startup at 08:00 is not the broad slot in hourly mode. The independent
    # 18:xx slot can recover at startup if this process wasn't running at 18:xx.
    with warehouse() as db:
        assert db.scalar(select(SyncJob)) is None
    assert jobs["daily_pipeline"][1].get_next_fire_time(None, due) == broad_due
    assert jobs["daily_pipeline_recovery"][1].interval == timedelta(minutes=1)
    clock = broad_due + timedelta(minutes=5)
    scheduler.main()
    with warehouse() as db:
        row = db.scalar(select(SyncJob))
        assert row.metadata_json["idempotency_key"] == "scheduled-collection:2026-11-19"
    assert jobs["hourly_ad_monitor"][1].get_next_fire_time(None, due) == due
    assert jobs["hourly_ad_monitor_recovery"][1].interval == timedelta(minutes=1)
    for job_id in ("hourly_ad_monitor", "hourly_ad_monitor_recovery", "hourly_ad_monitor_startup"):
        callback, _, kwargs = jobs[job_id]
        assert callback is scheduler.hourly_ad_monitor_job and kwargs["executor"] == "ad_monitor"
    assert "feishu_daily_report" in jobs and "feishu_outbox" in jobs


def test_hourly_entry_point_leaves_source_freshness_to_collector(monkeypatch):
    calls = []
    monkeypatch.setattr(hourly_ad_monitor, "run_hourly_monitor",
                        lambda **kwargs: calls.append(kwargs) or {"status": "ALREADY_COMPLETE"})
    scheduler.hourly_ad_monitor_job()
    assert calls == [{}]
