from datetime import datetime, timezone
import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications import worker
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.outbox import enqueue_notification, NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store


def test_expired_report_backlog_does_not_block_today_or_send_before_nine(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "outbox.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"default_chat_id": "report", "groups": {
        "report": {"chat_id": "oc_test"}}}), encoding="utf-8")
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app", feishu_timeout=1,
        feishu_routing_file=str(routing), feishu_daily_report_chat_id="report")
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sent = []
    def send(text, *, before_submit, **kw):
        before_submit({})
        sent.append(text)
        return {"message_id": "om_test"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda _: SimpleNamespace(send_text=send))
    instant = datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc)  # 14th 07:30 Beijing

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    delivery = {"role": "daily_snapshot", "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []}
    with sessions() as db:
        db.add(Store(sid='5174', name='Geteen', status=1))
        for n in range(7):
            row = enqueue_notification(db, idempotency_key=f"scheduled-daily:old{n}", text="旧日报", delivery=delivery)
            row.payload = {**row.payload, "scheduled_delivery_date": "2026-11-13", "scheduled_timezone": "Asia/Shanghai"}
            row.next_attempt_at = datetime(2026, 11, 13)
        today = enqueue_notification(db, idempotency_key="scheduled-daily:today", text="今日日报", store_id='5174', delivery=delivery)
        today.payload = {**today.payload, "scheduled_delivery_date": "2026-11-14", "scheduled_timezone": "Asia/Shanghai"}
        today.next_attempt_at = datetime(2026, 11, 13)
        enqueue_notification(db, idempotency_key="normal-alert", text="普通异常", delivery=delivery).next_attempt_at = datetime(2026, 11, 13)
        db.commit()
    assert worker.process_outbox_batch()["sent"] == 1
    assert sent == ["普通异常"]
    with sessions() as db:
        rows = db.scalars(select(NotificationOutbox)).all()
        assert sum(r.status == "EXPIRED" for r in rows) == 7
        assert next(r for r in rows if r.payload["text"] == "今日日报").status == "PENDING"
    instant = datetime(2026, 11, 14, 1, tzinfo=timezone.utc)
    assert worker.process_outbox_batch()["sent"] == 1
    assert sent == ["普通异常", "今日日报"]
    assert worker.process_outbox_batch()["sent"] == 0
    engine.dispose()


def test_malformed_scheduled_metadata_expires_and_normal_alerts_are_unrestricted():
    now = datetime(2026, 11, 14, 0)
    assert worker._scheduled_window("normal", {}, now)[0] == "READY"
    for payload in [{}, {"scheduled_delivery_date": "invalid"},
                    {"scheduled_delivery_date": "2026-11-14", "scheduled_timezone": "UTC"}]:
        assert worker._scheduled_window("scheduled-daily:test", payload, now)[0] == "EXPIRED"


def test_focused_daily_window_opens_at_nine_and_closes_at_eighteen():
    key = "scheduled-daily:morning:test"
    payload = {"scheduled_delivery_date": "2026-11-14", "scheduled_timezone": "Asia/Shanghai"}
    window = worker._scheduled_window
    assert window(key, payload, datetime(2026, 11, 14, 0, 59, 59))[0] == "WAITING"
    assert window(key, payload, datetime(2026, 11, 14, 1))[0] == "READY"
    assert window(key, payload, datetime(2026, 11, 14, 9, 59, 59))[0] == "READY"
    assert window(key, payload, datetime(2026, 11, 14, 10))[0] == "EXPIRED"
    assert window(key, payload, datetime(2026, 11, 15, 1))[0] == "EXPIRED"


def test_legacy_material_ad_stays_in_its_daylight_window_with_hourly_mode_enabled():
    key = "anomaly:advertising"
    payload = {"material_ad_alert": True}
    created = datetime(2026, 11, 14, 9, 45)
    window = worker._scheduled_window
    assert window(key, payload, datetime(2026, 11, 14, 10), created_at=created)[0] == "READY"
    assert window(key, payload, datetime(2026, 11, 14, 9, 59), hourly_mode=True, created_at=created)[0] == "READY"
    assert window(key, payload, datetime(2026, 11, 14, 10), hourly_mode=True, created_at=created)[0] == "EXPIRED"
    assert window(key, payload, datetime(2026, 11, 15, 0), hourly_mode=True, created_at=created)[0] == "EXPIRED"
    NIGHT = datetime(2026, 11, 14, 23, 30)
    assert window(key, payload, datetime(2026, 11, 15, 1), hourly_mode=True, created_at=NIGHT)[0] == "EXPIRED"
    reply = {**payload, "transport": "app", "card": {"schema": "2.0"},
             "delivery": {"role": "private_question", "reply_to_message_id": "om_test"}}
    assert window("group-question:om_test:reply", reply, NIGHT, hourly_mode=True)[0] == "READY"
    assert window("app:hourly-batch:test", {"hourly_ad_monitor": True}, NIGHT,
                  hourly_mode=True)[0] == "WAITING"


def test_focused_daily_retry_rechecks_cutoff_before_feishu_and_never_resends(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "quiet-retry.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app", feishu_timeout=1,
        feishu_routing_file=str(tmp_path / "routing.json"), hourly_ad_monitor_enabled=True)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    attempts = []

    def send(text, **kwargs):
        attempts.append(text)
        raise RuntimeError("simulated pre-submit token service error")

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda _: SimpleNamespace(send_text=send))
    instant = datetime(2026, 11, 14, 9, 59, 20, tzinfo=timezone.utc)
    jump_after_claim = None

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            nonlocal instant, jump_after_claim
            current = instant
            if jump_after_claim is not None:
                instant, jump_after_claim = jump_after_claim, None
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    delivery = {"role": "daily_snapshot", "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []}
    with sessions() as db:
        db.add(Store(sid="5174", name="Geteen", status=1))
        row = enqueue_notification(db, idempotency_key="scheduled-daily:morning:retry", text="日报",
                                   store_id="5174", delivery=delivery)
        row.payload = {**row.payload, "scheduled_delivery_date": "2026-11-14", "scheduled_timezone": "Asia/Shanghai"}
        row.next_attempt_at = instant.replace(tzinfo=None)
        row_id = row.id
        db.commit()

    worker.process_outbox_batch()
    with sessions() as db:
        assert db.get(NotificationOutbox, row_id).status == "PRE_SEND_RETRY"
    instant = instant.replace(second=59)
    jump_after_claim = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)
    assert worker.process_outbox_batch()["sent"] == 0
    with sessions() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "EXPIRED" and row.claimed_at is None
    instant = datetime(2026, 11, 15, 1, tzinfo=timezone.utc)
    assert worker.process_outbox_batch()["sent"] == 0
    assert attempts == ["日报"]
    engine.dispose()
