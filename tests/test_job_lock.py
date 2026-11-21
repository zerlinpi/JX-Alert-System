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


@pytest.mark.parametrize("disconnect", [False, True])
def test_pinned_mysql_lock_leaves_transaction_to_caller_and_does_not_reconnect(disconnect):
    from types import SimpleNamespace

    calls = []

    class Connection:
        invalidated = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def scalar(self, statement, params):
            calls.append(str(statement))
            return 1

        def execute(self, statement, params):
            assert not self.invalidated
            calls.append(str(statement))

        def commit(self):
            calls.append("commit")

    connection = Connection()
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="jxalert"),
        connect=lambda: connection,
    )
    with database_job_lock(engine, "campaign_sp_example", pin_connection=True) as locked:
        assert locked is connection
        assert calls == ["SELECT GET_LOCK(:key, 0)", "commit"]
        locked.invalidated = disconnect
    assert calls[2:] == ([] if disconnect else ["SELECT RELEASE_LOCK(:key)"])
