"""Thread replies retain their original question, permission boundary and retry identity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications.cards import card
from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications import worker
from jx_alert.storage.db import Base
from jx_alert.storage.models import NotificationLog


def answer():
    return card("店铺分析", subtitle="测试店铺", sections=[
        ("广告指标", ["指标", "观测值"], [("CTR", "0.35%"), ("订单", "0")])])


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("expired_status", [200, 401])
def test_thread_reply_uses_official_path_no_recipient_query_and_stable_refresh_uuid(monkeypatch, expired_status):
    tokens, replies = [], []
    def handler(request):
        if request.url.path.endswith("/internal"):
            token = "test-token-" + str(len(tokens) + 1)
            tokens.append(token)
            return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": 7200})
        assert request.method == "POST"
        assert request.url == "https://open.feishu.cn/open-apis/im/v1/messages/om_original/reply"
        assert not request.url.params
        payload = json.loads(request.content)
        assert payload["msg_type"] == "interactive" and payload["reply_in_thread"] is True
        assert "receive_id" not in payload and "receive_id_type" not in payload
        assert json.loads(payload["content"]) == answer()
        replies.append((payload, request.headers["Authorization"]))
        if len(replies) == 1:
            return httpx.Response(expired_status, json={"code": 99991663, "msg": "expired"})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_reply"}})
    mock_http(monkeypatch, handler)
    client = FeishuAppClient("cli_test", "test-secret")
    assert client.reply_card("om_original", answer(), idempotency_key="question-1") == {"message_id": "om_reply"}
    client.reply_card("om_original", answer(), idempotency_key="question-1")
    assert len(tokens) == 2 and len(replies) == 3
    assert len({value[0]["uuid"] for value in replies}) == 1
    assert len(replies[0][0]["uuid"]) == 40
    assert replies[0][1] == "Bearer test-token-1"
    assert all(value[1] == "Bearer test-token-2" for value in replies[1:])


@pytest.mark.parametrize("message_id", ["", None, "om_", "om_original/../messages", "om_%2f", "om_a\n", "https://other.invalid"])
def test_reply_rejects_invalid_message_ids_before_network(monkeypatch, message_id):
    client = FeishuAppClient("cli_test", "test-secret")
    monkeypatch.setattr(client, "_get_token", lambda: pytest.fail("Invalid recipient cannot acquire a token"))
    with pytest.raises(ValueError, match="message ID"):
        client.reply_card(message_id, answer(), idempotency_key="question-1")


@pytest.mark.parametrize("key", ["", "  ", None])
def test_reply_requires_retry_identity_before_network(monkeypatch, key):
    client = FeishuAppClient("cli_test", "test-secret")
    monkeypatch.setattr(client, "_get_token", lambda: pytest.fail("Missing key cannot acquire a token"))
    with pytest.raises(ValueError, match="idempotency"):
        client.reply_card("om_original", answer(), idempotency_key=key)


def test_reply_enforces_wire_size_limit_before_authentication(monkeypatch):
    client = FeishuAppClient("cli_test", "test-secret")
    monkeypatch.setattr(client, "_get_token", lambda: pytest.fail("Oversized answer cannot acquire a token"))
    oversized = deepcopy(answer())
    oversized["body"]["elements"].append({"tag": "markdown", "content": "数据" * 15000})
    with pytest.raises(ValueError, match="20 KB"):
        client.reply_card("om_original", oversized, idempotency_key="question-1")


@pytest.fixture
def warehouse(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "delivery.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app", feishu_timeout=5)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    yield sessions
    engine.dispose()


def pending(sessions, *, role="group_question", with_card=True, original="om_original"):
    with sessions() as db:
        payload = {"text": "店铺回复", "transport": "app", "fixed_route": True, "open_ids": [],
            "delivery": {"role": role, "receive_id": "oc_reports", "receive_id_type": "chat_id",
                         "open_ids": [], "reply_to_message_id": original}}
        if with_card:
            payload["card"] = answer()
        row = NotificationOutbox(idempotency_key="question-answer", chat_id="oc_reports", payload=payload)
        db.add(row)
        db.commit()
        return row.id


def test_worker_question_delivery_replies_in_thread_and_records_confirmed_message(warehouse, monkeypatch):
    calls = []
    class Client:
        def reply_card(self, message_id, value, **kwargs):
            calls.append((message_id, value, kwargs))
            return {"message_id": "om_confirmed_reply"}
        def send_card(self, *args, **kwargs):
            pytest.fail("Question answers must use their original thread")
        def send_text(self, *args, **kwargs):
            pytest.fail("Question answers must use cards")
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: Client())
    row_id = pending(warehouse)
    assert worker.process_outbox_batch(outbox_ids=[row_id])["sent"] == 1
    assert calls == [("om_original", answer(), {"idempotency_key": "question-answer"})]
    with warehouse() as db:
        row = db.get(NotificationOutbox, row_id)
        log = db.scalar(select(NotificationLog))
        assert row.status == "DELIVERED" and row.delivered_at is not None and row.claimed_at is None
        assert log.status == "DELIVERED" and log.channel == "feishu_app"
        assert log.message_id == "om_confirmed_reply" and log.error is None
    assert worker.process_outbox_batch(outbox_ids=[row_id])["claimed"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("role,with_card", [("daily_snapshot", True), ("chat_analysis", True), ("group_question", False)])
def test_nonquestion_or_noncard_delivery_cannot_reply(warehouse, monkeypatch, role, with_card):
    class Client:
        def reply_card(self, *args, **kwargs):
            pytest.fail("Unverified deliveries must not reply")
        def send_card(self, *args, **kwargs):
            pytest.fail("Invalid thread delivery must not fall back to ordinary send")
        def send_text(self, *args, **kwargs):
            pytest.fail("Invalid thread delivery must not fall back to ordinary send")
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: Client())
    row_id = pending(warehouse, role=role, with_card=with_card)
    result = worker.process_outbox_batch(outbox_ids=[row_id])
    assert result["sent"] == 0 and result["failed"] == 1
    with warehouse() as db:
        assert db.get(NotificationOutbox, row_id).status == "RETRY"
        log = db.scalar(select(NotificationLog))
        assert log.status == "RETRY" and log.message_id is None


def test_retry_retains_original_question_and_key_without_exposing_provider_error(warehouse, monkeypatch):
    calls = []
    class Client:
        def reply_card(self, message_id, value, **kwargs):
            calls.append((message_id, kwargs))
            if len(calls) == 1:
                raise FeishuAppError("private-provider-content")
            return {"message_id": "om_confirmed_reply"}
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: Client())
    row_id = pending(warehouse)
    assert worker.process_outbox_batch(outbox_ids=[row_id])["failed"] == 1
    with warehouse() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "RETRY" and "private-provider-content" not in row.last_error
        row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()
    assert worker.process_outbox_batch(outbox_ids=[row_id])["sent"] == 1
    assert calls == [("om_original", {"idempotency_key": "question-answer"})] * 2
