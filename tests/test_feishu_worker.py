from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import NotificationLog
import jx_alert.notifications.worker as worker


def test_worker_recovers_sending_row_without_claim_timestamp(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        db.add(NotificationOutbox(
            idempotency_key="stale-job",
            chat_id="default",
            payload={"text": "test alert", "open_ids": []},
            status="SENDING",
            attempts=1,
            next_attempt_at=datetime.utcnow() - timedelta(minutes=10),
            claimed_at=None,
        ))
        db.commit()

    sent = []

    class FakeFeishuClient:
        def __init__(self, **kwargs):
            pass

        def send_text(self, message, open_ids=None):
            sent.append((message, open_ids))
            return {"code": 0}

    settings = SimpleNamespace(
        app_dry_run=False,
        feishu_enabled=True,
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        feishu_sign_secret="",
        feishu_keyword="",
        feishu_timeout=10,
        feishu_routing_file=str(tmp_path / "missing-routing.json"),
        feishu_at_ids=lambda: [],
    )
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "FeishuClient", FakeFeishuClient)

    result = worker.process_outbox_batch()

    assert result["sent"] == 1
    assert result["claimed"] == 1
    assert sent == [("test alert", [])]
    with Session() as db:
        row = db.scalar(select(NotificationOutbox))
        assert row.status == "DELIVERED"
        assert row.claimed_at is None
        log = db.scalar(select(NotificationLog))
        assert log.status == "DELIVERED"
