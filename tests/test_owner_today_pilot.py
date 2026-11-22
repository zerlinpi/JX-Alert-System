"""One-shop acceptance: real inventory sync, real CLI, synthetic inputs, denied sockets."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.operations import sync_scoped_store_inventory
from jx_alert.reporting.owner_pilot import PilotBlocked
from jx_alert.storage.db import Base
from test_owner_today import FIELDS, FINANCE, PACIFIC, PRODUCT, ROOT, SCRIPT, audit_cli, partition

UTC = timezone.utc
MIDNIGHT = datetime(2024, 11, 22, 6, 30, tzinfo=UTC)  # Nov 21 in LA, Nov 22 in NY
FOLD = datetime(2024, 11, 3, 9, 30, tzinfo=UTC)  # Second 01:30 in LA
NY = "America/New_York"


def stamp(t):
    return t.isoformat().replace("+00:00", "Z")


def write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def bundle(tmp_path, *, cutoff=MIDNIGHT, authority=PACIFIC, synced=True, since=None,
           delay=timedelta(0)):
    d = SimpleNamespace(db=tmp_path / "sealed #?.sqlite", scope=tmp_path / "scope.json",
        capture=tmp_path / "capture.json", proof=tmp_path / "authority.json",
        config=tmp_path / "example-config.json", cutoff=cutoff)
    write(d.scope, {"store_ids": ["101"]})
    write(d.config, {"stores": {"101": {"timezone": PACIFIC}}})
    settings = SimpleNamespace(collection_scope_file=d.scope, store_timezones_file=d.config)
    local = cutoff.astimezone(ZoneInfo(PACIFIC)).date()
    ends = {PRODUCT: cutoff - delay - timedelta(minutes=8), FINANCE: cutoff - delay - timedelta(minutes=4)}
    records = {"data": [{"sid": 101, "name": "synthetic", "status": 1}]}
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(d.db))
    with Session(engine) as db:
        Base.metadata.create_all(engine)
        sync_scoped_store_inventory(db, settings, records)
        partition(db, "101", PRODUCT, facts=FIELDS, at=ends[PRODUCT], day=local)
        partition(db, "101", FINANCE, facts={"totalSalesRefunds": -7}, at=ends[FINANCE], day=local)
        db.commit()
        write(d.config, {"stores": {} if authority is None else {"101": {"timezone": authority}}})
        if synced:
            sync_scoped_store_inventory(db, settings, records)
        db.commit()
    engine.dispose()
    proof = {"schema": "owner-pilot-zone/v1", "store_id": "101", "timezone": authority,
             "confirmed": authority in {PACIFIC, NY}, "confirmed_through_utc": stamp(cutoff),
             "effective_since_utc": stamp(since or cutoff - timedelta(days=4)),
             "acquisitions": {k: {"started_at_utc": stamp(v - timedelta(minutes=2)),
                                  "warehouse_completed_at_utc": stamp(v)} for k, v in ends.items()}}
    write(d.proof, proof)
    write(d.capture, {"schema": "owner-pilot-capture/v1", "store_id": "101", "timezone": authority,
          "cutoff_utc": stamp(cutoff), "evidence_sha256": digest(d.proof), "sqlite_sha256": digest(d.db)})
    # Loaded before the real script starts. Also poison ambient config below.
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\ndef offline(e,a):\n if e.startswith('socket.'): raise AssertionError(e)\n"
        "sys.addaudithook(offline)\n")
    return d


def run(d, *, cutoff=True, capture=True, proof=True):
    flags = [sys.executable, str(SCRIPT), "--sqlite-copy", str(d.db), "--scope-file", str(d.scope)]
    if cutoff:
        flags.extend(("--as-of", stamp(d.cutoff) if cutoff is True else cutoff))
    if capture:
        flags.extend(("--pilot-capture", str(d.capture)))
    if proof:
        flags.extend(("--timezone-evidence", str(d.proof)))
    folder = d.db.parent
    def all_hashes():
        return {p.name: digest(p) for p in folder.iterdir()}
    before = all_hashes()
    result = subprocess.run(flags, capture_output=True, text=True, cwd=folder, timeout=20,
        env={"PATH": os.defpath, "HOME": str(folder), "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPATH": os.pathsep.join((str(folder), str(ROOT / "src"))), "DATABASE_URL": "invalid",
             "STORE_TIMEZONES_FILE": str(d.config)})
    assert before == all_hashes()
    assert not result.stderr
    data = json.loads(result.stdout)
    assert data["provider_updated_at"] is None and data["delivery_enabled"] is False
    return result.returncode, data


def blocked(d, reason, **opts):
    code, data = run(d, **opts)
    assert code == 1 and data == {"status": "PILOT_BLOCKED", "reason": reason, "groups": [],
                                  "provider_updated_at": None, "delivery_enabled": False}


@pytest.mark.parametrize("cutoff", [MIDNIGHT, FOLD])
def test_matching_earlier_capture_is_independent_of_later_configuration(tmp_path, cutoff):
    d = bundle(tmp_path, cutoff=cutoff)
    write(d.config, {"stores": {"101": {"timezone": NY}}})  # Never consult today's mapping.
    code, report = run(d, cutoff=cutoff.astimezone(ZoneInfo("Asia/Shanghai")).isoformat())
    assert code == 0 and report["pilot_gate"] == "MATCHED_OPERATOR_CAPTURE"
    assert report["mode"] == "READ_ONLY_REPLAY" and report["reference_cutoff_at"] == cutoff.isoformat()
    group, = report["groups"]
    assert group["report_date"] == cutoff.astimezone(ZoneInfo(PACIFIC)).date().isoformat()
    assert group["timezone"] == PACIFIC and len(group["cards"]) == 2
    assert list(group["stores"][0]["metrics"].values()) == ["PROVISIONAL_VALUE"] * 4
    if cutoff == FOLD:  # Identical 01:30 LA wall clock, different UTC instant.
        blocked(d, "CUTOFF_MISMATCH", cutoff=stamp(cutoff-timedelta(hours=1)))


@pytest.mark.parametrize("authority,sync,reason", [
    (None, True, "ZONE_NOT_CONFIRMED"), ("Bad/Timezone", True, "ZONE_NOT_CONFIRMED"),
    (NY, False, "INVENTORY_ZONE_MISMATCH"),
])
def test_stale_valid_zone_can_pass_legacy_but_cannot_qualify_pilot(tmp_path, authority, sync, reason):
    d = bundle(tmp_path, authority=authority, synced=sync)
    code, report = run(d, capture=False, proof=False)
    assert code == 0 and report["status"] == "ALL_METRICS_PROVISIONAL" and "pilot_gate" not in report
    assert report["groups"][0]["report_date"] == "2024-11-21"
    blocked(d, reason)


def test_changed_synced_zone_does_not_borrow_previous_local_date_receipts(tmp_path):
    d = bundle(tmp_path, authority=NY)
    blocked(d, "ACQUISITION_RECEIPT_MISSING")


def test_bound_capture_does_not_turn_stale_receipts_into_provisional_values(tmp_path):
    code, report = run(bundle(tmp_path, delay=timedelta(hours=2)))
    assert code == 2 and report["pilot_gate"] == "MATCHED_OPERATOR_CAPTURE"
    assert report["status"] == "NO_PREVIEW_VALUES"
    group, = report["groups"]
    assert not group["cards"] and set(group["stores"][0]["metrics"].values()) == {"STALE_CAPTURE"}


def test_synchronized_zone_still_cannot_relabel_existing_same_date_sources(tmp_path):
    t = MIDNIGHT + timedelta(hours=8)  # LA and NY both Nov 22.
    # Finance began under the new basis, but PRODUCT started before the effective change.
    d = bundle(tmp_path, cutoff=t, authority=NY, since=t-timedelta(minutes=9))
    code, report = run(d, capture=False, proof=False)
    assert code == 0 and report["groups"][0]["timezone"] == NY
    blocked(d, "ACQUISITION_OUTSIDE_CONFIRMED_INTERVAL")


@pytest.mark.parametrize("target,key,value,reason", [
    ("capture", "cutoff_utc", "2024-11-23T06:30:00Z", "CUTOFF_MISMATCH"),
    ("capture", "sqlite_sha256", "a"*64, "SNAPSHOT_DIGEST_MISMATCH"),
    ("capture", "sqlite_sha256", "not-a-hash", "INVALID_SNAPSHOT_DIGEST"),
    ("proof", "confirmed_through_utc", "2024-11-23T06:30:00Z", "CUTOFF_MISMATCH"),
    ("proof", "confirmed_through_utc", "2024-11-22T06:30:00", "INVALID_RECORD_TIME"),
    ("proof", "confirmed", 1, "ZONE_NOT_CONFIRMED"),
    ("proof", "store_id", "102", "STORE_SCOPE_MISMATCH"),
    ("proof", "timezone", NY, "ZONE_NOT_CONFIRMED"),
    ("proof", "acquisitions", {PRODUCT: {}}, "ACQUISITION_BASIS_MISSING"),
    ("scope", "store_ids", ["101", "102"], "INVALID_PILOT_SCOPE"),
    ("scope", "store_ids", [True], "INVALID_PILOT_SCOPE"),
])
def test_bad_operator_records_cannot_release_cards(tmp_path, target, key, value, reason):
    d = bundle(tmp_path)
    path = getattr(d, target)
    write(path, {**json.loads(path.read_bytes()), key: value})
    if target == "proof":
        write(d.capture, {**json.loads(d.capture.read_bytes()), "evidence_sha256": digest(d.proof)})
    blocked(d, reason)


def test_proof_tampering_and_missing_or_malformed_inputs_never_downgrade(tmp_path):
    d = bundle(tmp_path)
    for opts in ({"capture": False}, {"proof": False}, {"cutoff": False}):
        blocked(d, "EXPLICIT_CAPTURE_EVIDENCE_AND_CUTOFF_REQUIRED", **opts)
    d.proof.write_bytes(d.proof.read_bytes() + b" ")
    blocked(d, "EVIDENCE_DIGEST_MISMATCH")
    d.proof.unlink()
    blocked(d, "INVALID_ZONE_EVIDENCE")
    d.capture.write_bytes(b'{"store_id":"102",' + d.capture.read_bytes()[1:])
    blocked(d, "INVALID_CAPTURE_RECORD")


def test_independent_receipt_for_each_source_and_active_inventory_required(tmp_path):
    d = bundle(tmp_path)
    p = json.loads(d.proof.read_bytes())
    p["acquisitions"][FINANCE]["warehouse_completed_at_utc"] = stamp(d.cutoff)
    write(d.proof, p)
    write(d.capture, {**json.loads(d.capture.read_bytes()), "evidence_sha256": digest(d.proof)})
    blocked(d, "ACQUISITION_RECEIPT_MISMATCH")
    with sqlite3.connect(d.db) as conn:
        conn.execute("UPDATE stores SET status=2 WHERE sid='101'")
    blocked(d, "SNAPSHOT_DIGEST_MISMATCH")
    write(d.capture, {**json.loads(d.capture.read_bytes()), "sqlite_sha256": digest(d.db)})
    blocked(d, "NO_ACTIVE_INVENTORY")


@pytest.mark.parametrize("change,reason", [
    ("DELETE FROM data_coverage WHERE dimension=?", "ACQUISITION_RECEIPT_MISSING"),
    ("UPDATE data_coverage SET completed_at=x'6162' WHERE dimension=?", "UNUSABLE_INPUT"),
])
def test_missing_or_unreadable_second_receipt_blocks_the_whole_shop(tmp_path, change, reason):
    d = bundle(tmp_path)
    with sqlite3.connect(d.db) as conn:
        conn.execute(change, (FINANCE,))
    write(d.capture, {**json.loads(d.capture.read_bytes()), "sqlite_sha256": digest(d.db)})
    blocked(d, reason)


@pytest.mark.parametrize("cutoff", [datetime(2024, 1, 1), datetime.min.replace(tzinfo=UTC),
                                    datetime.max.replace(tzinfo=UTC), "invalid",
                                    MIDNIGHT+timedelta(seconds=1)])
def test_direct_pilot_rejects_invalid_clock_before_opening_inputs(tmp_path, cutoff):
    missing = tmp_path / "missing"
    with pytest.raises(PilotBlocked, match="INVALID_PILOT_CUTOFF"):
        audit_cli.audit(missing, missing, now=MIDNIGHT, as_of=cutoff,
                        pilot_capture=missing, timezone_evidence=missing)
    assert not list(tmp_path.iterdir())


def test_regular_copy_replaced_during_replay_never_releases_result(tmp_path, monkeypatch):
    d = bundle(tmp_path)
    original = audit_cli.prepare_owner_today
    def replace(*args, **kwargs):
        result = original(*args, **kwargs)
        moved = tmp_path / "original.sqlite"
        d.db.rename(moved)
        d.db.write_bytes(moved.read_bytes())  # Even identical bytes are not stable custody.
        return result
    monkeypatch.setattr(audit_cli, "prepare_owner_today", replace)
    with pytest.raises(PilotBlocked, match="SNAPSHOT_CHANGED"):
        audit_cli.audit(d.db, d.scope, as_of=d.cutoff, pilot_capture=d.capture, timezone_evidence=d.proof)
