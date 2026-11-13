"""Scheduled reminder identity and presentation of incomplete warehouse facts."""
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.notifications.daily_report import build_daily_card, enqueue_daily_snapshots
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store


DAY = date(2026, 11, 11)
DUE = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path):
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"default_chat_id": "test", "groups": {
        "test": {"chat_id": "oc_test"}}}), encoding="utf-8")
    settings = SimpleNamespace(feishu_daily_report_enabled=True, feishu_transport="app",
                               feishu_daily_report_chat_id="test", feishu_routing_file=str(routing),
                               feishu_daily_report_cron="0 8 * * *", feishu_daily_report_start_date="")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(sid="5174", name="Store", timezone="America/Los_Angeles", currency_code="USD")
        db.add(store)
        db.flush()
        yield db, settings, store
    engine.dispose()


def enqueue(db, settings, *, now=DUE, delivery_day="2026-11-13"):
    return enqueue_daily_snapshots(db, settings, store_dates={"5174": DAY},
                                   delivery_date=delivery_day, now=now)


def test_no_scheduled_row_before_deadline_or_activation_date(setup):
    db, settings, _ = setup
    assert enqueue(db, settings, now=DUE - timedelta(microseconds=1))["reason"] == "before_scheduled_time"
    settings.feishu_daily_report_start_date = "2026-11-14"
    assert enqueue(db, settings)["reason"] == "before_start_date"
    assert db.scalars(select(NotificationOutbox)).all() == []


def test_scheduled_identity_is_delivery_day_and_does_not_rearm_delivered_retry_or_manual(setup):
    db, settings, _ = setup
    manual = enqueue_daily_snapshots(db, settings, report_date=DAY)
    first = enqueue(db, settings)
    assert first["outbox_ids"] != manual["outbox_ids"]
    row = db.get(NotificationOutbox, first["outbox_ids"][0])
    assert row.next_attempt_at == DUE.replace(tzinfo=None)
    assert row.payload["scheduled_delivery_date"] == "2026-11-13"
    assert row.payload["scheduled_timezone"] == "Asia/Shanghai"
    row.status, row.attempts = "DELIVERED", 2
    row.next_attempt_at = DUE.replace(tzinfo=None) + timedelta(hours=2)
    db.flush()
    second = enqueue(db, settings, now=DUE + timedelta(hours=3))
    assert second["outbox_ids"] == first["outbox_ids"]
    assert (row.status, row.attempts) == ("DELIVERED", 2)
    assert row.next_attempt_at == DUE.replace(tzinfo=None) + timedelta(hours=2)
    tomorrow = enqueue(db, settings, now=DUE + timedelta(days=1), delivery_day="2026-11-14")
    assert tomorrow["outbox_ids"] != first["outbox_ids"]
    assert len(db.scalars(select(NotificationOutbox)).all()) == 3


def test_late_generation_is_labelled_and_invalid_clocks_or_past_days_are_rejected(setup):
    db, settings, _ = setup
    result = enqueue(db, settings, now=DUE + timedelta(hours=1))
    payload = db.get(NotificationOutbox, result["outbox_ids"][0]).payload
    assert "延迟补发" in payload["text"] and "09:00:00" in payload["text"]
    assert "延迟补发" in json.dumps(payload["card"], ensure_ascii=False)
    with pytest.raises(ValueError, match="aware"):
        enqueue(db, settings, now=DUE.replace(tzinfo=None))
    with pytest.raises(ValueError, match="Past delivery"):
        enqueue(db, settings, now=DUE + timedelta(days=1))


def test_incomplete_partition_values_masked_but_complete_explicit_zero_is_real(setup):
    db, settings, store = setup
    for day, orders, status in [(DAY, 0, "COMPLETE"), (DAY - timedelta(days=1), 991, "FAILED")]:
        db.add(DailyMetric(store_id=store.sid, entity_id=store.sid, metric_date=day,
                           dimension="store", metric_name="orders", value=orders))
        db.add(DataCoverage(store_id=store.sid, metric_date=day, dimension="store", status=status))
    db.add(DailyMetric(store_id=store.sid, entity_id="campaign", metric_date=DAY,
                       dimension="campaign_sp", metric_name="spend", value=999))
    db.add(DataCoverage(store_id=store.sid, metric_date=DAY, dimension="campaign_sp", status="FAILED"))
    db.flush()
    result = enqueue(db, settings)
    payload = db.get(NotificationOutbox, result["outbox_ids"][0]).payload
    assert "订单 0" in payload["text"]
    assert "991" not in json.dumps(payload) and "999" not in json.dumps(payload)
    table = next(e for e in payload["card"]["body"]["elements"] if e["tag"] == "table")
    assert table["rows"][0]["c1"] == "0"
    assert table["rows"][0]["c2"] == "未知（无记录）"
    assert table["rows"][0]["c3"] == "待完整采集"
    warned = build_daily_card(db, store, DAY, data_warning="时区配置已更新，等待重新采集校验")
    warned_table = next(e for e in warned["body"]["elements"] if e["tag"] == "table")
    assert warned_table["rows"][0]["c1"] == "未知（无记录）"

