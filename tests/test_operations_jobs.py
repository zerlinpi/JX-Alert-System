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
    with warehouse() as db:
        db.add(SyncJob(job_type="manual_collection", status="RUNNING", metadata_json={
            "attempts": 0, "backfill_days": 1}))
        db.commit()
    monkeypatch.setattr(operations, "run_collection", lambda **kwargs: {"status": "COMPLETE"})
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
