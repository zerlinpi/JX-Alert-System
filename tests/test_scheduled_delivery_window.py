from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications import worker
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.outbox import enqueue_notification, NotificationOutbox
from jx_alert.storage.db import Base


def test_expired_report_backlog_does_not_block_today_or_send_before_eight(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "outbox.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app", feishu_timeout=1)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sent = []
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda _: SimpleNamespace(
        send_text=lambda text, **kw: sent.append(text) or {"message_id": "om_test"}))
    instant = datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc)  # 14th 07:30 Beijing

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    delivery = {"role": "daily_snapshot", "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []}
    with sessions() as db:
        for n in range(7):
            row = enqueue_notification(db, idempotency_key=f"scheduled-daily:old{n}", text="旧日报", delivery=delivery)
            row.payload = {**row.payload, "scheduled_delivery_date": "2026-11-13", "scheduled_timezone": "Asia/Shanghai"}
            row.next_attempt_at = datetime(2026, 11, 13)
        today = enqueue_notification(db, idempotency_key="scheduled-daily:today", text="今日日报", delivery=delivery)
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
    instant = datetime(2026, 11, 14, 0, tzinfo=timezone.utc)
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
