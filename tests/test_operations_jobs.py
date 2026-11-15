from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import operations
from jx_alert.storage.db import Base
from jx_alert.storage.models import SyncJob


@pytest.fixture
def warehouse(monkeypatch, tmp_path):
    from jx_alert.storage import db
    engine = create_engine("sqlite:///" + str(tmp_path / "test.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    return sessions


def test_enqueue_reuses_pending_and_explicit_success_key(warehouse):
    with warehouse() as db:
        first = operations.enqueue_collection(db, idempotency_key="report-1")
        again = operations.enqueue_collection(db, idempotency_key="report-1")
        assert first["job_id"] == again["job_id"]
        db.get(SyncJob, first["job_id"]).status = "SUCCEEDED"
        db.commit()
        assert operations.enqueue_collection(db, idempotency_key="report-1")["reused"]
        with pytest.raises(ValueError):
            operations.enqueue_collection(db, backfill_days=22, idempotency_key="report-1")


def test_quality_warning_is_explicit_and_does_not_retry_successful_collection(warehouse, monkeypatch):
    monkeypatch.setattr(operations, "run_collection", lambda **kwargs: {"status": "COMPLETE_WITH_WARNINGS"})
    with warehouse() as db:
        job_id = operations.enqueue_collection(db, idempotency_key="quality-warning")["job_id"]
        db.commit()
    result = operations.process_pending_collection()
    assert result == {"job_id": job_id, "status": "SUCCEEDED_WITH_WARNINGS"}
    with warehouse() as db:
        job = db.get(SyncJob, job_id)
        assert job.metadata_json["attempts"] == 0
        assert "retry_at" not in job.metadata_json
    assert operations.process_pending_collection()["status"] == "IDLE"


@pytest.mark.parametrize("bad", [0, True, 31, -1])
def test_invalid_backfill_never_enqueues(warehouse, bad):
    with warehouse() as db, pytest.raises(ValueError):
        operations.enqueue_collection(db, backfill_days=bad)


def test_future_retry_does_not_block_ready_job(warehouse, monkeypatch):
    with warehouse() as db:
        db.add(SyncJob(job_type="manual_collection", status="PENDING", metadata_json={
            "retry_at": (datetime.now(timezone.utc).replace(tzinfo=None)
                         + timedelta(hours=1)).isoformat()}))
        ready = operations.enqueue_collection(db, backfill_days=2)
        db.commit()
    monkeypatch.setattr(operations, "run_collection", lambda **kwargs: {"status": "COMPLETE"})
    result = operations.process_pending_collection()
    assert result == {"job_id": ready["job_id"], "status": "SUCCEEDED"}


def test_crashed_running_job_recovers_under_lock(warehouse, monkeypatch):
    clock = datetime(2026, 11, 13, 20, 0)
    monkeypatch.setattr(operations, "_utcnow", lambda: clock)
    with warehouse() as db:
        db.add(SyncJob(job_type="manual_collection", status="RUNNING", metadata_json={
            "attempts": 0, "backfill_days": 1}))
        db.commit()
    monkeypatch.setattr(operations, "run_collection", lambda **kwargs: {"status": "COMPLETE"})
    assert operations.process_pending_collection()["status"] == "IDLE"
    with warehouse() as db:
        job = db.scalar(select(SyncJob))
        assert job.status == "PENDING"
        assert job.metadata_json["retry_at"] == (clock + timedelta(minutes=1)).isoformat()
        assert job.metadata_json["error_type"] == "InterruptedCollection"
    clock += timedelta(minutes=1)
    assert operations.process_pending_collection()["status"] == "SUCCEEDED"
    with warehouse() as db:
        job = db.scalar(select(SyncJob))
        assert job.metadata_json["attempts"] == 1
        assert "error_type" not in job.metadata_json


def test_partial_result_has_bounded_retry_without_sending(warehouse, monkeypatch):
    with warehouse() as db:
        operations.enqueue_collection(db)
        db.commit()
    monkeypatch.setattr(operations, "run_collection", lambda **kwargs: {"status": "PARTIAL"})
    assert operations.process_pending_collection()["status"] == "PENDING"
    with warehouse() as db:
        job = db.scalar(select(SyncJob))
        assert job.metadata_json["attempts"] == 1
        assert job.metadata_json["retry_at"]


def test_retries_keep_the_original_store_dates_and_wait_between_attempts(warehouse, monkeypatch):
    clock = datetime(2026, 11, 13, 23, 59, 50)
    planned = {"5174": "2026-11-12", "5175": "2026-11-13"}
    completed = {"5174": "2026-11-12"}
    monkeypatch.setattr(operations, "_utcnow", lambda: clock)
    with warehouse() as db:
        job = SyncJob(job_type="manual_collection", status="PENDING",
                      metadata_json={"store_ids": ["5174", "5175"], "attempts": 0})
        db.add(job)
        db.commit()
        job_id = job.id
    calls = []

    def partial(**kwargs):
        assert kwargs["store_ids"] == ["5174", "5175"]
        if not calls:
            kwargs["progress"]({
                "planned_store_dates": planned, "completed_store_dates": completed,
            })
        else:
            assert kwargs["planned_store_dates"] == planned
            assert kwargs["completed_store_dates"] == completed
        calls.append(clock)
        return {"status": "PARTIAL", "completed_store_ids": ["5174"],
                "completed_store_dates": completed, "failed_store_ids": ["5175"]}

    monkeypatch.setattr(operations, "run_collection", partial)
    for wait_seconds in (60, 120):
        assert operations.process_pending_collection() == {"job_id": job_id, "status": "PENDING"}
        with warehouse() as db:
            assert db.get(SyncJob, job_id).metadata_json["retry_at"] == (
                clock + timedelta(seconds=wait_seconds)).isoformat()
        clock += timedelta(seconds=wait_seconds - 1)
        assert operations.process_pending_collection()["status"] == "IDLE"
        clock += timedelta(seconds=1)
    assert operations.process_pending_collection() == {"job_id": job_id, "status": "PARTIAL_FAILED"}
    with warehouse() as db:
        job = db.get(SyncJob, job_id)
        assert job.finished_at == clock
        assert job.metadata_json["planned_store_dates"] == planned
        assert "retry_at" not in job.metadata_json
    assert operations.process_pending_collection()["status"] == "IDLE"
    assert len(calls) == 3


def test_legacy_partial_job_reuses_its_recorded_report_dates(warehouse, monkeypatch):
    planned = {"5174": "2026-11-12", "5175": "2026-11-13"}
    completed = {"5174": "2026-11-12"}
    with warehouse() as db:
        job = SyncJob(job_type="manual_collection", status="PENDING", metadata_json={
            "attempts": 1, "result": {"status": "PARTIAL", "report_dates": planned},
            "completed_store_dates": completed,
        })
        db.add(job)
        db.commit()

    def resume(**kwargs):
        assert kwargs["planned_store_dates"] == planned
        assert kwargs["completed_store_dates"] == completed
        return {"status": "COMPLETE"}

    monkeypatch.setattr(operations, "run_collection", resume)
    assert operations.process_pending_collection()["status"] == "SUCCEEDED"
    with warehouse() as db:
        assert db.get(SyncJob, job.id).metadata_json["planned_store_dates"] == planned


@pytest.mark.parametrize("reason", [None, "WAITING_TIMEZONE"])
def test_configuration_blocked_job_does_not_consume_retries(warehouse, monkeypatch, reason):
    def blocked(**kwargs):
        if reason:
            raise operations.CollectionBlocked(reason)
        return {"status": "CONFIGURATION_BLOCKED"}

    monkeypatch.setattr(operations, "run_collection", blocked)
    with warehouse() as db:
        job_id = operations.enqueue_collection(db)["job_id"]
        db.commit()
    assert operations.process_pending_collection() == {"job_id": job_id, "status": "BLOCKED"}
    with warehouse() as db:
        job = db.get(SyncJob, job_id)
        assert job.metadata_json["attempts"] == 0
        assert "retry_at" not in job.metadata_json
        assert job.metadata_json["result"].get("reason") == reason
    assert operations.process_pending_collection()["status"] == "IDLE"


def test_store_progress_is_durable_before_a_later_collection_crash(warehouse, monkeypatch):
    with warehouse() as db:
        job_id = operations.enqueue_collection(db)["job_id"]
        db.commit()

    def interrupted(**kwargs):
        kwargs["progress"]({"completed_store_dates": {"2": "2026-11-11"},
                            "store_statuses": {"2": "COMPLETE"}})
        with warehouse() as db:
            saved = db.get(SyncJob, job_id)
            assert saved.status == "RUNNING"
            assert saved.metadata_json["completed_store_dates"] == {"2": "2026-11-11"}
        raise RuntimeError("later provider failure")

    monkeypatch.setattr(operations, "run_collection", interrupted)
    assert operations.process_pending_collection()["status"] == "PENDING"
    with warehouse() as db:
        assert db.get(SyncJob, job_id).metadata_json["completed_store_dates"] == {"2": "2026-11-11"}


def test_store_scope_rejects_missing_or_zero_ids(tmp_path):
    path = tmp_path / "scope.json"
    for ids in [[0], [True], [5175]]:
        path.write_text(json.dumps({"store_ids": ids}))
        with pytest.raises(ValueError):
            operations.scoped_stores({"data": [{"sid": 5174}]}, path)


def test_completed_date_check_blocks_in_progress_marketplace_day():
    with pytest.raises(ValueError):
        operations._completed_dates("2026-11-11", {"5174": "2026-11-10"})
    assert operations._completed_dates("2026-11-10", {"5174": "2026-11-10"}) == {
        "5174": "2026-11-10"}
