import json
import stat
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import RawSnapshot
from jx_alert.storage.raw_archive import archive_raw_snapshots


def test_archive_raw_snapshots_is_atomic_private_and_idempotent(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(RawSnapshot(
            source="../../lingxing/performance:list",
            store_id="../../101",
            captured_at=datetime(2026, 11, 11, 10, 20, 30, 123456),
            payload={"data": [{"subject": "政策通知", "status": 1}]},
        ))
        db.commit()

        first = archive_raw_snapshots(db, tmp_path)
        assert first["written"] == 1
        assert first["already_present"] == 0
        files = list(tmp_path.rglob("*.json"))
        assert len(files) == 1
        assert tmp_path.resolve() in files[0].resolve().parents
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        assert saved["schema_version"] == 1
        assert saved["source"] == "../../lingxing/performance:list"
        assert saved["store_id"] == "../../101"
        assert saved["payload"]["data"][0]["status"] == 1

        second = archive_raw_snapshots(db, tmp_path)
        assert second["written"] == 0
        assert second["already_present"] == 1
        assert len(list(tmp_path.rglob("*.json"))) == 1
