from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jx_alert import mcp_server
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import NotificationLog


def test_group_report_delivery_is_visible_for_each_exact_member_store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    monkeypatch.setattr(mcp_server, "SessionLocal", sessions)
    monkeypatch.setattr(mcp_server, "init_db", lambda: None)
    with sessions() as db:
        for key, payload in [("single", {"store_id": "5174"}),
                             ("digest", {"store_ids": ["5174", "5175"]}),
                             ("other", {"store_ids": ["15174", "51740"]})]:
            db.add(NotificationOutbox(idempotency_key=key, payload=payload, status="DELIVERED"))
            db.add(NotificationLog(dedupe_key=key, channel="feishu_app", status="DELIVERED"))
        db.commit()
    assert {r["dedupe_key"] for r in mcp_server.get_notification_logs(store_id="5174")} == {"single", "digest"}
    assert {r["dedupe_key"] for r in mcp_server.get_notification_logs(store_id="5175")} == {"digest"}
    assert mcp_server.get_notification_logs(store_id="74") == []
    engine.dispose()
