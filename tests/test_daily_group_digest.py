"""Scheduled multi-store cards preserve unknown data and retry identity."""
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.notifications.daily_report import enqueue_daily_snapshots
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store


@pytest.fixture
def warehouse(tmp_path):
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"default_chat_id": "test", "groups": {"test": {"chat_id": "oc_test"}}}))
    settings = SimpleNamespace(feishu_daily_report_enabled=True, feishu_transport="app",
        feishu_daily_report_chat_id="test", feishu_routing_file=str(routing),
        feishu_daily_report_group_digest=True, feishu_daily_report_cron="0 8 * * *")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for i in range(64):
            db.add(Store(sid=str(i + 1), name="测试店铺" + str(i + 1), timezone=None, currency_code="BRL"))
        db.flush()
        yield db, settings
    engine.dispose()


def enqueue(db, settings, **kwargs):
    return enqueue_daily_snapshots(db, settings, report_date=date(2026, 11, 11),
        delivery_date=date(2026, 11, 14), now=datetime(2026, 11, 14, tzinfo=timezone.utc), **kwargs)


def test_group_pages_bound_size_freeze_scope_and_do_not_rearm(warehouse):
    db, settings = warehouse
    result = enqueue(db, settings)
    assert result["queued"] == 7
    rows = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
    assert len({sid for r in rows for sid in r.payload["store_ids"]}) == 64
    for row in rows:
        assert row.idempotency_key.startswith("scheduled-daily:")
        assert row.payload["scheduled_delivery_date"] == "2026-11-14"
        assert len(json.dumps(row.payload["card"], ensure_ascii=False).encode()) < 24000
        assert "未知（无记录）" in json.dumps(row.payload["card"], ensure_ascii=False)
        assert "时区待核实" in json.dumps(row.payload["card"], ensure_ascii=False)
        row.status = "DELIVERED"
    original = json.dumps([r.payload for r in rows], sort_keys=True)
    db.add(Store(sid="99", name="新店铺"))
    db.flush()
    assert enqueue(db, settings)["outbox_ids"] == result["outbox_ids"]
    assert all(r.status == "DELIVERED" for r in rows)
    assert json.dumps([r.payload for r in rows], sort_keys=True) == original


def test_digest_respects_warning_and_explicit_zero_and_avoids_legacy_duplicate(warehouse):
    db, settings = warehouse
    day = date(2026, 11, 11)
    db.add_all([DailyMetric(store_id="1", entity_id="1", dimension="store", metric_date=day,
                          metric_name="orders", value=0),
                DailyMetric(store_id="1", entity_id="1", dimension="store", metric_date=day,
                          metric_name="sales", value=12.3, currency="BRL"),
                DataCoverage(store_id="1", dimension="store", metric_date=day, status="COMPLETE"),
                DailyMetric(store_id="2", entity_id="2", dimension="store", metric_date=day,
                          metric_name="orders", value=999),
                DataCoverage(store_id="2", dimension="store", metric_date=day, status="COMPLETE")])
    db.flush()
    result = enqueue(db, settings, store_warnings={"2": "授权失效"})
    all_rows = [r for id in result["outbox_ids"] for e in db.get(NotificationOutbox, id).payload["card"]["body"]["elements"]
                if e["tag"] == "table" for r in e["rows"]]
    assert next(r for r in all_rows if "（1）" in r["c0"])["c2"] == "0"
    assert next(r for r in all_rows if "（1）" in r["c0"])["c4"] == "巴西雷亚尔 12.30"
    assert next(r for r in all_rows if "（2）" in r["c0"])["c2"] == "未知（无记录）"
    assert "999" not in json.dumps(all_rows)


def test_no_early_digest_and_legacy_rows_are_reused(warehouse):
    db, settings = warehouse
    early = enqueue_daily_snapshots(db, settings, report_date="2026-11-11", delivery_date="2026-11-14",
        now=datetime(2026, 11, 13, 23, 59, tzinfo=timezone.utc))
    assert early["reason"] == "before_scheduled_time"
    settings.feishu_daily_report_group_digest = False
    legacy = enqueue(db, settings)
    settings.feishu_daily_report_group_digest = True
    assert enqueue(db, settings)["outbox_ids"] == legacy["outbox_ids"]
