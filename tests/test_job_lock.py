import pytest
from sqlalchemy import create_engine

from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock


def test_two_connections_to_same_sqlite_file_cannot_run_simultaneously(tmp_path):
    url = f"sqlite:///{tmp_path / 'warehouse.db'}"
    first = create_engine(url)
    second = create_engine(url)
    with database_job_lock(first):
        with pytest.raises(JobAlreadyRunning):
            with database_job_lock(second):
                pytest.fail("overlapping database job")
    with database_job_lock(second):
        pass


def test_job_lock_releases_when_job_raises():
    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(ValueError):
        with database_job_lock(engine):
            raise ValueError("job failed")
    with database_job_lock(engine):
        with pytest.raises(JobAlreadyRunning):
            with database_job_lock(engine):
                pass


def test_mysql_uses_connection_scoped_advisory_lock():
    from types import SimpleNamespace

    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def scalar(self, statement, params):
            calls.append(str(statement))
            return 1

        def execute(self, statement, params):
            calls.append(str(statement))

    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="jxalert"),
        connect=lambda: Connection(),
    )
    with database_job_lock(engine):
        assert len(calls) == 1
    assert "GET_LOCK" in calls[0]
    assert "RELEASE_LOCK" in calls[1]
