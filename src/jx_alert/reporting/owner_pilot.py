"""Offline consistency gate for one operator-sealed owner-day pilot, never a trust issuer."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.lingxing.api_business import FINANCE, PRODUCT
from jx_alert.storage.models import DataCoverage, Store


class PilotBlocked(ValueError):
    """Only fixed reason codes may be returned; never include paths, data or input strings."""


def _require(ok, reason):
    if not ok:
        raise PilotBlocked(reason)


def _unique_object(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("duplicate key")
    return data


def _record(path, keys, reason):
    # These are small private one-shop records, not a full deployment configuration.
    try:
        p = Path(path)
        if not p.is_file():
            raise ValueError
        with p.open("rb") as f:
            raw = f.read(16_385)
        if len(raw) > 16_384:
            raise ValueError
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(obj, dict) or set(obj) != set(keys):
            raise ValueError
    except (OSError, ValueError, RecursionError):
        raise PilotBlocked(reason) from None
    return obj, hashlib.sha256(raw).hexdigest()


def _utc(value):
    _require(isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value),
        "INVALID_RECORD_TIME")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise PilotBlocked("INVALID_RECORD_TIME") from None


@dataclass(frozen=True)
class PilotCapture:
    sid: str
    zone: ZoneInfo
    cutoff: datetime
    sqlite_sha256: str
    completions: dict[str, datetime]

    def check_inventory_and_sources(self, db):
        store = db.get(Store, self.sid)
        _require(store is not None and store.status == 1, "NO_ACTIVE_INVENTORY")
        _require(store.timezone == self.zone.key, "INVENTORY_ZONE_MISMATCH")
        # Only now is deriving a date safe. Do not render, date-relabel or inspect facts
        # until both acquisition windows also match their exact warehouse receipts.
        day = self.cutoff.astimezone(self.zone).date()
        receipts = db.scalars(select(DataCoverage).where(
            DataCoverage.store_id == self.sid, DataCoverage.metric_date == day,
            DataCoverage.dimension.in_((PRODUCT, FINANCE))).limit(3)).all()
        parts = {p.dimension: p for p in receipts}
        _require(len(parts) == len(receipts), "AMBIGUOUS_ACQUISITION_RECEIPT")
        for key, end in self.completions.items():
            p = parts.get(key)
            _require(p is not None and p.status == "COMPLETE" and
                     type(p.row_count) is int and p.row_count > 0 and
                     isinstance(p.completed_at, datetime), "ACQUISITION_RECEIPT_MISSING")
            recorded = p.completed_at
            if recorded.tzinfo is None:  # Warehouse timestamps are stored as UTC.
                recorded = recorded.replace(tzinfo=timezone.utc)
            _require(recorded == end, "ACQUISITION_RECEIPT_MISMATCH")


def load_pilot(scope_file, capture_file, evidence_file, cutoff, *, now=None):
    _require(capture_file is not None and evidence_file is not None and cutoff is not None,
             "EXPLICIT_CAPTURE_EVIDENCE_AND_CUTOFF_REQUIRED")
    generated = now if now is not None else datetime.now(timezone.utc)
    try:
        if any(not isinstance(t, datetime) or t.utcoffset() is None for t in (cutoff, generated)):
            raise ValueError
        cutoff, generated = (t.astimezone(timezone.utc) for t in (cutoff, generated))
        if not date.min < cutoff.date() < date.max or cutoff > generated:
            raise ValueError
    except (OverflowError, ValueError):
        raise PilotBlocked("INVALID_PILOT_CUTOFF") from None
    scope, _ = _record(scope_file, ("store_ids",), "INVALID_PILOT_SCOPE")
    ids = scope["store_ids"]
    _require(isinstance(ids, list) and len(ids) == 1 and type(ids[0]) in {str, int} and
             re.fullmatch(r"[1-9][0-9]{0,99}", str(ids[0])), "INVALID_PILOT_SCOPE")
    sid = str(ids[0])
    capture, _ = _record(capture_file,
        ("schema", "store_id", "timezone", "cutoff_utc", "sqlite_sha256", "evidence_sha256"),
        "INVALID_CAPTURE_RECORD")
    proof, digest = _record(evidence_file, ("schema", "store_id", "timezone", "confirmed",
        "effective_since_utc", "confirmed_through_utc", "acquisitions"), "INVALID_ZONE_EVIDENCE")
    _require(capture["schema"] == "owner-pilot-capture/v1" and
             proof["schema"] == "owner-pilot-zone/v1", "UNSUPPORTED_RECORD_SCHEMA")
    _require(capture["evidence_sha256"] == digest, "EVIDENCE_DIGEST_MISMATCH")
    _require(isinstance(capture["sqlite_sha256"], str) and
             re.fullmatch(r"[0-9a-f]{64}", capture["sqlite_sha256"]), "INVALID_SNAPSHOT_DIGEST")
    _require(capture["store_id"] == proof["store_id"] == sid, "STORE_SCOPE_MISMATCH")
    _require(_utc(capture["cutoff_utc"]) == _utc(proof["confirmed_through_utc"]) == cutoff,
             "CUTOFF_MISMATCH")
    name = proof["timezone"]
    _require(proof["confirmed"] is True and isinstance(name, str) and
             name == capture["timezone"], "ZONE_NOT_CONFIRMED")
    # Require a portable geographic IANA identifier (or UTC); never host-specific
    # localtime, POSIX rules, TZ environment defaults, or a numeric offset.
    _require(name == "UTC" or re.fullmatch(
        r"(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific)"
        r"(?:/[A-Za-z0-9_+-]+)+", name), "INVALID_REPORT_ZONE")
    try:
        zone = ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise PilotBlocked("INVALID_REPORT_ZONE") from None
    since = _utc(proof["effective_since_utc"])
    windows = proof["acquisitions"]
    _require(isinstance(windows, dict) and set(windows) == {PRODUCT, FINANCE},
             "ACQUISITION_BASIS_MISSING")
    completions = {}
    for dim, w in windows.items():
        _require(isinstance(w, dict) and set(w) == {"started_at_utc", "warehouse_completed_at_utc"},
                 "ACQUISITION_BASIS_MISSING")
        start, end = _utc(w["started_at_utc"]), _utc(w["warehouse_completed_at_utc"])
        _require(since <= start <= end <= cutoff, "ACQUISITION_OUTSIDE_CONFIRMED_INTERVAL")
        completions[dim] = end
    return PilotCapture(sid, zone, cutoff, capture["sqlite_sha256"], completions)


def _identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@contextmanager
def verify_sealed_copy(path, expected):
    """Operator controls immutable file and directory; detect ordinary replacements/edits."""
    with path.open("rb") as f:
        identity = _identity(os.fstat(f.fileno()))
        _require(hashlib.file_digest(f, "sha256").hexdigest() == expected, "SNAPSHOT_DIGEST_MISMATCH")
        _require(_identity(path.stat()) == identity, "SNAPSHOT_CHANGED")
        yield
        _require(_identity(os.fstat(f.fileno())) == _identity(path.stat()) == identity and
                 not any(Path(str(path) + ext).exists() for ext in ("-wal", "-shm", "-journal")),
                 "SNAPSHOT_CHANGED")
