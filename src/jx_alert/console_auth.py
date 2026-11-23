"""Single-owner console authentication: a PBKDF2 password file and hashed sessions.

The credential file keeps the format of the previous release (mcp-owner.json),
so the existing owner password continues to work. On POSIX both files must be
owner-owned, mode 0600, in a directory other users cannot modify.
"""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

ROUNDS = 600_000
SESSION_TTL = 1800
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def private_file(path: Path, *, create=False):
    if not path.is_absolute():
        raise ValueError("Console private file paths must be absolute")
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        info, parent = path.lstat(), path.parent.stat()
    except OSError:
        raise ValueError("Console private file is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or path.parent.is_symlink():
        raise ValueError("Console private files must be regular files, not symbolic links")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                               or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022):
        raise ValueError("Console private files require owner ownership and mode 0600 in a protected directory")


def create_owner_credentials(path, password):
    """Write a new PBKDF2 hash once. The caller must obtain the password securely."""
    if not isinstance(password, str) or not 16 <= len(password) <= 1024:
        raise ValueError("Owner password must contain 16 to 1024 characters")
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Console private file paths must be absolute")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ROUNDS).hex()
    payload = json.dumps({"algorithm": "pbkdf2_sha256", "rounds": ROUNDS, "salt": salt, "hash": derived})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
    private_file(path)


class OwnerPassword:
    def __init__(self, path):
        path = Path(path)
        private_file(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data["algorithm"] != "pbkdf2_sha256" or type(data["rounds"]) is not int
                    or not 600_000 <= data["rounds"] <= 2_000_000
                    or not re.fullmatch(r"[0-9a-f]{64}", data["salt"])
                    or not re.fullmatch(r"[0-9a-f]{64}", data["hash"])):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError("Owner credential file is invalid") from None
        self._data = data

    def verify(self, password):
        if not isinstance(password, str) or not 16 <= len(password) <= 1024:
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self._data["salt"]),
                                      self._data["rounds"]).hex()
        return hmac.compare_digest(derived, self._data["hash"])


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class SessionStore:
    """Opaque browser tokens; only their SHA-256 is stored."""

    def __init__(self, path):
        self.path = Path(path)

    def new(self):
        private_file(self.path, create=True)
        token, now = secrets.token_urlsafe(32), time.time()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL)")
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?, ?)", (_digest(token), now + SESSION_TTL))
        return token

    def valid(self, token):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token) or not self.path.exists():
            return False
        private_file(self.path)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL)")
            return db.execute("SELECT 1 FROM sessions WHERE id=? AND expires>?",
                              (_digest(token), time.time())).fetchone() is not None

    def remove(self, token):
        if isinstance(token, str) and _TOKEN.fullmatch(token) and self.path.exists():
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL)")
                db.execute("DELETE FROM sessions WHERE id = ?", (_digest(token),))
