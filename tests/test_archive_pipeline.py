from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.reporting.archive import archive_partitions, archive_sp_partition
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, SyncJob


def test_disk_failure_retains_facts_and_queues_one_independent_retry(tmp_path, monkeypatch):
    import jx_alert.reporting.excel as excel
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(excel_archive_enabled=True, excel_archive_dir=str(tmp_path / "excel"))
    def fail(*args, **kwargs):
        raise OSError("private-provider-content-must-not-be-logged")
    monkeypatch.setattr(excel, "archive_collected_store", fail)
    with Session(engine) as db:
        db.add(DailyMetric(store_id="5174", entity_id="5174", metric_date=date(2026, 11, 12),
            dimension="store", metric_name="sales", value=123, currency="USD"))
        db.commit()
        for _ in range(2):
            result = archive_partitions(db, settings, {"5174": ["2026-11-12"]})
            assert result["status"] == "RETRY_PENDING"
            assert result["errors"][0]["error_type"] == "OSError"
        jobs = db.scalars(select(SyncJob).where(SyncJob.job_type == "excel_archive")).all()
        assert len(jobs) == 1 and jobs[0].status == "PENDING"
        assert "private-provider" not in str(jobs[0].metadata_json)
        assert db.scalar(select(DailyMetric)).value == 123
        monkeypatch.setattr(excel, "archive_collected_store", lambda *args, **kwargs: [tmp_path / "latest.xlsx"])
        assert archive_partitions(db, settings, {"5174": "2026-11-12"})["status"] == "COMPLETE"
        assert db.get(SyncJob, jobs[0].id).status == "SUCCEEDED"
    engine.dispose()


def test_sp_disk_failure_is_retryable_without_recollecting_lingxing(tmp_path, monkeypatch):
    import jx_alert.reporting.excel as excel
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    def fail(*args):
        raise PermissionError("private-path")
    monkeypatch.setattr(excel, "archive_sp_run", fail)
    with Session(engine) as db:
        settings = SimpleNamespace(excel_archive_dir=str(tmp_path))
        for _ in range(2):
            assert archive_sp_partition(db, settings, "run-1")["status"] == "RETRY_PENDING"
        jobs = db.scalars(select(SyncJob)).all()
        assert len(jobs) == 1
        assert jobs[0].metadata_json["run_id"] == "run-1"
        assert jobs[0].metadata_json["kind"] == "sp"
    engine.dispose()
