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
# Owner (Nov 2026): the 30-minute session logged out too soon. A session now lives while it is
# used: every request renews it for SESSION_IDLE, up to SESSION_MAX after the login.
SESSION_IDLE = 12 * 3600
SESSION_MAX = 7 * 24 * 3600
SESSION_TTL = SESSION_MAX  # the browser cookie lasts as long as the longest session
RENEW_AFTER = 300
LEGACY_TTL = 1800  # sessions created by earlier releases
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_SCHEMA = ("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL)",
           # Added table (earlier releases keep using ``sessions`` unchanged, also after a rollback).
           "CREATE TABLE IF NOT EXISTS session_starts (id TEXT PRIMARY KEY, created REAL NOT NULL)")


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
    """Opaque browser tokens; only their SHA-256 is stored.

    A valid request renews the session (at most every RENEW_AFTER seconds) to SESSION_IDLE from
    now, never past SESSION_MAX after the login; the token itself stays the same, so the page's
    CSRF token stays valid.
    """

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock

    def _open(self):
        db = sqlite3.connect(self.path, timeout=5)
        for statement in _SCHEMA:
            db.execute(statement)
        return db

    def new(self):
        private_file(self.path, create=True)
        token, now = secrets.token_urlsafe(32), self.clock()
        with closing(self._open()) as db, db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute("DELETE FROM session_starts WHERE id NOT IN (SELECT id FROM sessions)")
            db.execute("INSERT INTO sessions VALUES (?, ?)", (_digest(token), now + SESSION_IDLE))
            db.execute("INSERT OR REPLACE INTO session_starts VALUES (?, ?)", (_digest(token), now))
        return token

    def valid(self, token):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token) or not self.path.exists():
            return False
        private_file(self.path)
        key, now = _digest(token), self.clock()
        with closing(self._open()) as db, db:
            found = db.execute("SELECT s.expires, t.created FROM sessions s LEFT JOIN session_starts t ON t.id = s.id "
                               "WHERE s.id = ?", (key,)).fetchone()
            if found is None:
                return False
            expires, created = found
            created = created if created is not None else expires - LEGACY_TTL
            limit = created + SESSION_MAX
            if expires <= now or limit <= now:
                return False
            if expires - now < SESSION_IDLE - RENEW_AFTER:
                db.execute("UPDATE sessions SET expires = ? WHERE id = ?", (min(now + SESSION_IDLE, limit), key))
                db.execute("INSERT OR IGNORE INTO session_starts VALUES (?, ?)", (key, created))
            return True

    def remove(self, token):
        if isinstance(token, str) and _TOKEN.fullmatch(token) and self.path.exists():
            with closing(self._open()) as db, db:
                db.execute("DELETE FROM sessions WHERE id = ?", (_digest(token),))
                db.execute("DELETE FROM session_starts WHERE id = ?", (_digest(token),))
