"""Owner-edited private JSON files: 0600, lock-serialized, atomically replaced."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile

from jx_alert.storage.job_lock import _try_file_lock, _unlock_file


class Busy(RuntimeError):
    """Another writer holds the lock."""


def read_json(path: Path, *, max_bytes=1024 * 1024):
    """The parsed document, None when absent; ValueError when unsafe or unreadable."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe path")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("unreadable") from None
    if len(raw) > max_bytes or (os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise ValueError("unsafe file")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("invalid json") from None


@contextmanager
def locked(path: Path):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise ValueError("unsafe lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe lock")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
        except OSError:
            raise Busy() from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def replace_json(path: Path, value):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
