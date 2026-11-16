from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import bot_service
from jx_alert.notifications import worker
from jx_alert.notifications.cards import card
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.routing import bind_group_delivery, group_member_mentions, report_group_delivery
from jx_alert.storage.db import Base
from jx_alert.storage.models import NotificationLog


@pytest.fixture
def delivery_runtime(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    changed = (now - timedelta(minutes=1)).isoformat()
    routing = {"default_chat_id": "ops", "groups": {
        "ops": {"chat_id": "oc_new", "notifications_changed_at": changed,
                "retired_chat_ids": {"oc_old": changed}, "verified_member_open_ids": ["ou_present"]},
        "other": {"chat_id": "oc_other"}}, "stores": {}, "entities": {}}
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(routing), encoding="utf-8")
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app",
        feishu_timeout=20, feishu_routing_file=str(path), feishu_daily_report_chat_id="ops",
        store_controls_file=str(tmp_path / "controls.json"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sent = []

    class Client:
        def send_card(self, value, **kwargs):
            sent.append(("send", kwargs))
            return {"message_id": "om_sent"}

        def reply_card(self, message_id, value, **kwargs):
            sent.append(("reply", {"message_id": message_id, **kwargs}))
            return {"message_id": "om_reply"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: Client())
    yield sessions, settings, routing, path, sent, now
    engine.dispose()


def _row(key, delivery, created):
    return NotificationOutbox(idempotency_key=key, chat_id=delivery["receive_id"],
        created_at=created.replace(tzinfo=None), payload={"transport": "app", "delivery": delivery,
            "card": card("测试报告", subtitle="", sections=[])})


def test_group_switch_stops_frozen_old_content_and_preserves_other_destinations(delivery_runtime):
    sessions, settings, routing, _, sent, now = delivery_runtime
    legacy = {"role": "daily_snapshot", "receive_id": "oc_old", "receive_id_type": "chat_id", "open_ids": []}
    old_bound = {**legacy, "routing_alias": "ops", "routing_changed_at": None}
    fresh = report_group_delivery(settings, routing, role="daily_snapshot")
    other = bind_group_delivery(routing, "other", {**legacy, "receive_id": "oc_other"})
    manager = {"role": "management", "receive_id": "ou_manager", "receive_id_type": "open_id", "open_ids": []}
    with sessions.begin() as db:
        for key, destination in [("legacy", legacy), ("old-bound", old_bound),
                                 ("fresh", fresh), ("other", other), ("manager", manager)]:
            db.add(_row(key, destination, now - timedelta(minutes=10) if key in {"legacy", "old-bound"} else now))
    result = worker.process_outbox_batch()
    assert result["sent"] == 3 and result["failed"] == 0
    assert {args["receive_id"] for _, args in sent} == {"oc_new", "oc_other", "ou_manager"}
    with sessions() as db:
        rows = {row.idempotency_key: row for row in db.scalars(select(NotificationOutbox))}
        assert rows["legacy"].status == rows["old-bound"].status == "SKIPPED"
        assert rows["legacy"].payload["delivery"]["receive_id"] == "oc_old"
        assert all(row.status == "DELIVERED" for key, row in rows.items() if key not in {"legacy", "old-bound"})
        assert len(list(db.scalars(select(NotificationLog).where(NotificationLog.status == "SKIPPED")))) == 2


def test_switching_back_does_not_revive_old_legacy_or_generation(delivery_runtime):
    sessions, settings, routing, path, sent, now = delivery_runtime
    old = report_group_delivery(settings, routing, role="chat_report")
    switch = (now - timedelta(seconds=5)).isoformat()
    routing["groups"]["ops"].update(chat_id="oc_old", notifications_changed_at=switch,
        retired_chat_ids={"oc_new": switch, "oc_old": (now - timedelta(minutes=1)).isoformat()})
    path.write_text(json.dumps(routing), encoding="utf-8")
    legacy = {"role": "chat_report", "receive_id": "oc_old", "receive_id_type": "chat_id", "open_ids": []}
    with sessions.begin() as db:
        db.add(_row("old-new", old, now - timedelta(minutes=2)))
        db.add(_row("old-original", legacy, now - timedelta(minutes=2)))
        db.add(_row("fresh-back", report_group_delivery(settings, routing, role="chat_report"), now))
    assert worker.process_outbox_batch()["sent"] == 1
    assert sent[0][1]["receive_id"] == "oc_old"
    with sessions() as db:
        assert {row.idempotency_key: row.status for row in db.scalars(select(NotificationOutbox))} == {
            "old-new": "SKIPPED", "old-original": "SKIPPED", "fresh-back": "DELIVERED"}


def test_private_reply_stays_in_original_conversation_and_revocation_blocks_send(delivery_runtime, monkeypatch):
    sessions, _, _, _, sent, now = delivery_runtime
    monkeypatch.setattr(bot_service, "validate_question_delivery", lambda s, d: d["sender_open_id"] == "ou_allowed")
    with sessions.begin() as db:
        for key, sender in [("allowed", "ou_allowed"), ("revoked", "ou_revoked")]:
            destination = {"role": "private_question", "receive_id": "oc_private", "receive_id_type": "chat_id",
                "open_ids": [], "sender_open_id": sender, "reply_to_message_id": "om_" + key}
            db.add(_row(key, destination, now))
    assert worker.process_outbox_batch()["sent"] == 1
    assert sent == [("reply", {"message_id": "om_allowed", "idempotency_key": "allowed", "reply_in_thread": False})]
    with sessions() as db:
        assert db.scalar(select(NotificationOutbox).where(NotificationOutbox.idempotency_key == "revoked")).status == "SKIPPED"


def test_reply_api_uses_quote_in_private_chat_and_reuses_uuid(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_reply"}})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    client = FeishuAppClient("cli_test", "synthetic-secret")
    for _ in range(2):
        assert client.reply_card("om_original", card("私聊回复", subtitle="", sections=[]),
            idempotency_key="same-private-reply", reply_in_thread=False) == {"message_id": "om_reply"}
    replies = [json.loads(r.content) for r in requests if r.url.path.endswith("/om_original/reply")]
    assert len(replies) == 2 and all(r["reply_in_thread"] is False for r in replies)
    assert replies[0]["uuid"] == replies[1]["uuid"]
    assert not any("receive_id_type" in r.url.params for r in requests)


def test_new_group_mentions_preserve_only_verified_members_without_mutating_owners():
    owners = ["ou_absent", "ou_present", "ou_present"]
    assert group_member_mentions({"verified_member_open_ids": ["ou_present"]}, owners) == ["ou_present"]
    assert owners == ["ou_absent", "ou_present", "ou_present"]
    assert group_member_mentions({}, owners) == ["ou_absent", "ou_present"]


def test_quiet_mode_skips_queued_noise_without_sending_or_retrying(delivery_runtime, monkeypatch):
    from zoneinfo import ZoneInfo
    sessions, settings, routing, _, sent, now = delivery_runtime
    settings.feishu_quiet_delivery_enabled = True
    monkeypatch.setattr(bot_service, "validate_question_delivery", lambda s, d: True)
    group = report_group_delivery(settings, routing, role="daily_snapshot")
    reply = {**group, "role": "group_question", "reply_to_message_id": "om_question"}
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    due = {"scheduled_delivery_date": day, "scheduled_timezone": "Asia/Shanghai",
           "scheduled_not_before": day + "T00:00:00+08:00"}
    scheduled = {
        "scheduled-daily:morning:a": {"morning_message_kind": "health"},
        "scheduled-daily:morning:b": {"morning_message_kind": "ad_exception"},
        "scheduled-daily:morning:c": {"card": card("广告判断待补齐数据", subtitle="", sections=[])},
    }
    other = {
        "app:old-freshness": {},
        "chat-analysis:old-unsupported": {},
        "chat-report:manual": {},
        "store-health-brief:duplicate": {},
        "chat-ad-kpi:verified": {"material_ad_alert": True},
    }
    with sessions.begin() as db:
        for key, extra in {**scheduled, **other}.items():
            row = _row(key, group, now)
            row.payload = {**row.payload, **(due if key in scheduled else {}), **extra}
            db.add(row)
        db.add(_row("group-question:om_question:0", reply, now))
        db.add(_row("group-question:om_other:0", reply, now))
    for _ in range(2):
        worker.process_outbox_batch()
    assert len(sent) == 4 and [operation for operation, _ in sent].count("reply") == 1
    pending = {*scheduled, *other, "group-question:om_question:0", "group-question:om_other:0"}
    expected = {"scheduled-daily:morning:a", "scheduled-daily:morning:b",
                "chat-ad-kpi:verified", "group-question:om_question:0"}
    with sessions() as db:
        rows = {row.idempotency_key: row for row in db.scalars(select(NotificationOutbox))}
        assert {key for key, row in rows.items() if row.status == "DELIVERED"} == expected
        assert {key for key, row in rows.items() if row.status == "SKIPPED"} == pending - expected
        logs = list(db.scalars(select(NotificationLog).where(NotificationLog.status == "SKIPPED")))
        assert len(logs) == 6 and all(row.message_id is None for row in logs)
