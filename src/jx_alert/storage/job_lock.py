"""Process-safe, crash-released mutex for jobs sharing the same warehouse.

MariaDB/MySQL use a connection-scoped advisory lock. SQLite uses an OS lock
beside its database file; in-memory test databases use an engine-scoped mutex.
There is no expiring lease that could admit a second still-running collector.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

from sqlalchemy import text


class JobAlreadyRunning(RuntimeError):
    pass


_memory_locks: dict[tuple[object, str], threading.Lock] = {}
_memory_lock_guard = threading.Lock()


def _try_file_lock(fd: int):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def database_job_lock(engine, name: str = "daily_pipeline"):
    """Fail immediately if another process is running this warehouse job."""
    if not name or len(name) > 40 or not all(c.isalnum() or c == "_" for c in name):
        raise ValueError("Job lock name must be 1-40 letters, digits or underscores")
    if engine.dialect.name in {"mysql", "mariadb"}:
        with engine.connect() as connection:
            # The same MySQL server may host several independent warehouses.
            import hashlib
            database_key = hashlib.sha256(str(engine.url.database).encode()).hexdigest()[:16]
            key = f"jxalert:{database_key}:{name}"
            acquired = connection.scalar(text("SELECT GET_LOCK(:key, 0)"), {"key": key})
            if acquired != 1:
                raise JobAlreadyRunning(f"Job is already running: {name}")
            try:
                yield
            finally:
                try:
                    connection.execute(text("SELECT RELEASE_LOCK(:key)"), {"key": key})
                except Exception:
                    # Never return a still-locked physical connection to the pool.
                    connection.invalidate()
                    raise
        return
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Job locking supports MariaDB/MySQL and SQLite only")

    database = engine.url.database
    if not database or database == ":memory:":
        with _memory_lock_guard:
            lock = _memory_locks.setdefault((engine, name), threading.Lock())
        if not lock.acquire(blocking=False):
            raise JobAlreadyRunning(f"Job is already running: {name}")
        try:
            yield
        finally:
            lock.release()
        return

    path = Path(database).resolve()
    lock_path = path.with_name(path.name + f".{name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
            acquired = True
        except OSError as exc:
            raise JobAlreadyRunning(f"Job is already running: {name}") from exc
        yield
    finally:
        if acquired:
            _unlock_file(fd)
        os.close(fd)
