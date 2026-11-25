"""Saved Excel copies of the lane messages: data/reports/<lane>/<month>/<file>.xlsx (0600).

A file is written once when its message is queued and never changed afterwards; the outbox
row keeps its path and SHA-256, and the worker sends it only when the bytes still match.
The console lists the files and lets the owner download them. Nothing here is deleted.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
FOLDERS = ("daily", "review", "hourly", "tests")
MAX_BYTES = 10 * 1024 * 1024  # Feishu accepts 30 MB; a report workbook is a few dozen KB
_PATH = re.compile(r"(daily|review|hourly|tests)/(\d{4}-\d{2})/([^/\\\x00-\x1f]{1,160}\.xlsx)")
_SHA = re.compile(r"[0-9a-f]{64}")


class ArchiveError(RuntimeError):
    """The file cannot be saved or is missing, changed or unsafe."""


def root(settings):
    return Path(getattr(settings, "report_archive_dir", "/app/data/reports"))


def _directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ArchiveError("unsafe archive directory")


def save(settings, folder, name, data, *, now):
    """{"path", "name", "sha256", "size"} of the saved file. The same name with other bytes gets 「_2」, 「_3」…"""
    if folder not in FOLDERS or not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ArchiveError("invalid archive file")
    month = now.astimezone(BEIJING).strftime("%Y-%m")
    digest = hashlib.sha256(data).hexdigest()
    base = root(settings)
    target_dir = base / folder / month
    try:
        for part in (base, base / folder, target_dir):
            _directory(part)
        stem, number = name[:-5] if name.endswith(".xlsx") else name, 1
        while True:
            candidate = f"{stem}.xlsx" if number == 1 else f"{stem}_{number}.xlsx"
            relative = f"{folder}/{month}/{candidate}"
            if not _PATH.fullmatch(relative):
                raise ArchiveError("invalid archive file name")
            path = target_dir / candidate
            if path.exists() or path.is_symlink():
                if not path.is_symlink() and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    break  # this exact file is already saved
                number += 1
                if number > 50:
                    raise ArchiveError("too many files with this name")
                continue
            fd, temporary = tempfile.mkstemp(prefix=".jx-report-", suffix=".tmp", dir=target_dir)
            try:
                with os.fdopen(fd, "wb") as stream:
                    if os.name != "nt":
                        os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    continue  # another writer took the name meanwhile; try again
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            break
    except OSError:
        raise ArchiveError("archive not writable") from None
    return {"path": relative, "name": candidate, "sha256": digest, "size": len(data)}


def _resolve(settings, relative):
    if not isinstance(relative, str) or not _PATH.fullmatch(relative):
        raise ArchiveError("invalid archive path")
    base = root(settings)
    path = base / relative
    for part in (base, path.parent.parent, path.parent, path):
        if part.is_symlink():
            raise ArchiveError("unsafe archive path")
    return path


def read(settings, meta):
    """The bytes of a saved file, only when its path, size and SHA-256 still match the queued record."""
    if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str) or not _SHA.fullmatch(meta["sha256"]):
        raise ArchiveError("invalid archive record")
    path = _resolve(settings, meta.get("path"))
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise ArchiveError("archive file missing")
        data = path.read_bytes()
    except OSError:
        raise ArchiveError("archive file missing") from None
    if hashlib.sha256(data).hexdigest() != meta["sha256"] or len(data) != meta.get("size", len(data)):
        raise ArchiveError("archive file changed")
    return data


def load(settings, relative):
    """(file name, bytes) for a console download."""
    path = _resolve(settings, relative)
    try:
        if not path.is_file():
            raise ArchiveError("archive file missing")
        data = path.read_bytes()
    except OSError:
        raise ArchiveError("archive file missing") from None
    if len(data) > MAX_BYTES:
        raise ArchiveError("archive file too large")
    return path.name, data


def listing(settings, *, limit=80):
    """The newest saved files: [{"path", "name", "lane", "month", "size", "saved_at"}]."""
    base = root(settings)
    found = []
    for folder in FOLDERS:
        try:
            months = sorted((item for item in (base / folder).iterdir() if item.is_dir() and not item.is_symlink()),
                            reverse=True)[:6]
        except OSError:
            continue
        for month in months:
            try:
                entries = list(month.iterdir())
            except OSError:
                continue
            for item in entries:
                relative = f"{folder}/{month.name}/{item.name}"
                if item.is_symlink() or not _PATH.fullmatch(relative):
                    continue
                try:
                    info = item.stat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found.append({"path": relative, "name": item.name, "lane": folder, "month": month.name,
                                  "size": info.st_size,
                                  "saved_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()})
    found.sort(key=lambda item: item["saved_at"], reverse=True)
    return found[:limit]
