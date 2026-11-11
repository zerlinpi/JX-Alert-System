"""Durable, atomic filesystem archive for raw Lingxing API snapshots."""
import json
import os
import re
import tempfile
from pathlib import Path

from sqlalchemy import select

from jx_alert.storage.models import RawSnapshot


def _safe_segment(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))
    cleaned = cleaned.strip("._")
    return (cleaned or "unknown")[:80]


def archive_raw_snapshots(db, raw_dir: str | Path, *, after_id: int = 0) -> dict:
    """Write committed RawSnapshot rows to immutable, permission-restricted JSON files.

    Paths are deterministic per snapshot ID. Re-running the archiver is safe:
    an existing completed file is not overwritten. Each file is first written
    to a same-directory temporary file and atomically renamed into place.
    """
    root = Path(raw_dir).expanduser()
    rows = db.scalars(
        select(RawSnapshot)
        .where(RawSnapshot.id > int(after_id))
        .order_by(RawSnapshot.id)
    ).all()
    written = 0
    existing = 0
    for row in rows:
        captured_at = row.captured_at
        if captured_at is None:
            raise RuntimeError(f"RawSnapshot {row.id} is missing captured_at")
        folder = (
            root
            / captured_at.strftime("%Y")
            / captured_at.strftime("%m")
            / captured_at.strftime("%d")
            / _safe_segment(row.source)
        )
        if row.store_id is not None:
            folder = folder / _safe_segment(row.store_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / (
            f"{captured_at.strftime('%H%M%S_%f')}_{int(row.id):012d}.json"
        )
        if target.exists():
            existing += 1
            continue

        document = {
            "schema_version": 1,
            "snapshot_id": row.id,
            "source": row.source,
            "store_id": row.store_id,
            "captured_at_utc": captured_at.isoformat(sep=" "),
            "payload": row.payload,
        }
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=folder,
                prefix=".jx-alert-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            os.chmod(target, 0o600)
            written += 1
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    return {
        "written": written,
        "already_present": existing,
        "examined": len(rows),
        "root": str(root),
    }
