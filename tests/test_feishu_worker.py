from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import NotificationLog, Product
import jx_alert.notifications.worker as worker
from jx_alert.store_controls import get_store_controls, save_store_controls


def test_reenabled_notifications_skip_old_queued_pages_and_send_other_store(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="webhook",
        feishu_timeout=10, feishu_routing_file=str(tmp_path / "none.json"),
        feishu_webhook_url="https://example.test/webhook", feishu_sign_secret="", feishu_keyword="",
        feishu_at_ids=lambda: [], store_controls_file=str(tmp_path / "store-controls.json"))
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sent = []

    class FakeFeishu:
        def __init__(self, **kwargs):
            pass

        def send_text(self, message, open_ids=None):
            sent.append(message)
            return {"code": 0}

    monkeypatch.setattr(worker, "FeishuClient", FakeFeishu)
    with sessions.begin() as db:
        db.add_all([
            NotificationOutbox(idempotency_key="old-page", payload={"text": "mixed", "store_ids": ["5174", "5175"]}),
            NotificationOutbox(idempotency_key="unpaused", payload={"text": "other store", "store_id": "5175"}),
        ])
    original = get_store_controls(settings, "5174")
    stopped = save_store_controls(settings, "5174", True, False, original["revision"])
    save_store_controls(settings, "5174", True, True, stopped["revision"])
    with sessions.begin() as db:
        db.add(NotificationOutbox(idempotency_key="new-alert", payload={"text": "new alert", "store_id": "5174"}))
    assert worker.process_outbox_batch()["sent"] == 2
    assert sent == ["other store", "new alert"]
    with sessions() as db:
        states = {row.idempotency_key: row.status for row in db.scalars(select(NotificationOutbox))}
        assert states == {"old-page": "SKIPPED", "unpaused": "DELIVERED", "new-alert": "DELIVERED"}
        assert db.scalar(select(NotificationLog.status).where(NotificationLog.dedupe_key == "old-page")) == "SKIPPED"


def test_worker_recovers_sending_row_without_claim_timestamp(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        db.add(Product(
            store_id="store-1",
            seller_sku="SKU-1",
            asin="B000000001",
            owner_open_ids=["ou_product_owner"],
        ))
        db.add(Product(
            store_id="store-1",
            seller_sku="SKU-2",
            asin="B000000001",
            owner_open_ids=["ou_brand_owner"],
        ))
        db.add(NotificationOutbox(
            idempotency_key="stale-job",
            chat_id="default",
            payload={
                "text": "test alert", "open_ids": [],
                "store_id": "store-1", "entity_id": "B000000001",
            },
            status="SENDING",
            attempts=1,
            next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10),
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
    assert sent == [("test alert", ["ou_product_owner", "ou_brand_owner"])]
    with Session() as db:
        row = db.scalar(select(NotificationOutbox))
        assert row.status == "DELIVERED"
        assert row.claimed_at is None
        log = db.scalar(select(NotificationLog))
        assert log.status == "DELIVERED"



def test_worker_sends_to_store_specific_group_and_mentions_group_admin(
    monkeypatch, tmp_path
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    routing_file = tmp_path / "routing.json"
    routing_file.write_text(__import__("json").dumps({
        "default_chat_id": "main",
        "groups": {
            "main": {"webhook_env": "FEISHU_WEBHOOK_URL"},
            "store-1-ops": {
                "webhook_env": "FEISHU_WEBHOOK_URL_STORE1",
                "admin_open_ids": ["ou_group_admin"],
            },
        },
        "stores": {
            "store-1": {
                "chat_id": "store-1-ops",
                "open_ids": ["ou_store_owner"],
            },
        },
    }), encoding="utf-8")
    with Session() as db:
        db.add(Product(
            store_id="store-1",
            seller_sku="SKU-1",
            asin="B000000001",
            owner_open_ids=["ou_product_owner"],
        ))
        db.add(NotificationOutbox(
            idempotency_key="store-routed-alert",
            chat_id="default",
            payload={
                "text": "store routed alert",
                "open_ids": [],
                "store_id": "store-1",
                "entity_id": "SKU-1",
            },
            status="PENDING",
            attempts=0,
            next_attempt_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()

    sent = []
    clients = []

    class FakeFeishuClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        def send_text(self, message, open_ids=None):
            sent.append((message, open_ids))
            return {"code": 0}

    monkeypatch.setenv(
        "FEISHU_WEBHOOK_URL_STORE1",
        "https://open.feishu.cn/open-apis/bot/v2/hook/store-1",
    )
    settings = SimpleNamespace(
        app_dry_run=False,
        feishu_enabled=True,
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/main",
        feishu_sign_secret="",
        feishu_keyword="",
        feishu_timeout=10,
        feishu_routing_file=str(routing_file),
        feishu_at_ids=lambda: ["ou_global_admin"],
    )
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "FeishuClient", FakeFeishuClient)

    result = worker.process_outbox_batch()
    assert result["sent"] == 1
    assert clients[0]["webhook_url"].endswith("/store-1")
    assert sent == [(
        "store routed alert",
        ["ou_global_admin", "ou_group_admin", "ou_store_owner", "ou_product_owner"],
    )]
    with Session() as db:
        row = db.scalar(select(NotificationOutbox))
        assert row.chat_id == "store-1-ops"
        assert row.status == "DELIVERED"
