import json
from datetime import datetime, timezone

import pytest

from jx_alert.storage.db import Base
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.models import Store
from jx_alert.storage.store_config import build_store_report_dates, sync_store_config


def test_report_dates_are_computed_in_each_marketplace_timezone(tmp_path):
    config_path = tmp_path / "store-timezones.json"
    config_path.write_text(json.dumps({
        "stores": {
            "101": {"timezone": "America/Los_Angeles", "currency_code": "USD"},
            "102": {"timezone": "Europe/London", "currency_code": "GBP"},
        }
    }), encoding="utf-8")
    stores = {"data": [
        {"sid": 101, "status": 1},
        {"sid": 102, "status": 1},
        {"sid": 103, "status": 0},
    ]}
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    dates = build_store_report_dates(stores, str(config_path), now=now)
    assert dates == {"101": "2026-11-10", "102": "2026-11-10"}


def test_active_store_without_timezone_fails_closed(tmp_path):
    config_path = tmp_path / "store-timezones.json"
    config_path.write_text('{"stores":{}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Missing timezone configuration"):
        build_store_report_dates({"data": [{"sid": 101, "status": 1}]}, str(config_path))


def test_store_timezone_and_currency_mapping_persists(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    config_path = tmp_path / "store-timezones.json"
    config_path.write_text(json.dumps({
        "stores": {"101": {"timezone": "America/Los_Angeles", "currency_code": "usd"}}
    }), encoding="utf-8")
    with Session() as db:
        db.add(Store(sid="101", name="Store A"))
        db.commit()
        assert sync_store_config(db, str(config_path)) == 1
        db.commit()
        store = db.scalar(select(Store))
        assert store.timezone == "America/Los_Angeles"
        assert store.currency_code == "USD"
