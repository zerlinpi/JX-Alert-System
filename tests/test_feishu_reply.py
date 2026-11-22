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
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"default_chat_id": "report", "groups": {
        "report": {"chat_id": "oc_reports"}}}), encoding="utf-8")
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app", feishu_timeout=5,
        feishu_bot_enabled=True, feishu_routing_file=str(routing), feishu_daily_report_chat_id="report")
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
        def reply_card(self, message_id, value, before_submit, **kwargs):
            before_submit({})
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
        assert db.get(NotificationOutbox, row_id).status == "PRE_SEND_RETRY"
        log = db.scalar(select(NotificationLog))
        assert log.status == "PRE_SEND_RETRY" and log.message_id is None


def test_retry_retains_original_question_and_key_without_exposing_provider_error(warehouse, monkeypatch):
    calls = []
    class Client:
        def reply_card(self, message_id, value, before_submit, **kwargs):
            calls.append((message_id, kwargs))
            if len(calls) == 1:
                raise FeishuAppError("private-token-service-content")
            before_submit({})
            return {"message_id": "om_confirmed_reply"}
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: Client())
    row_id = pending(warehouse)
    assert worker.process_outbox_batch(outbox_ids=[row_id])["failed"] == 1
    with warehouse() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "PRE_SEND_RETRY" and "private-token-service-content" not in row.last_error
        row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()
    assert worker.process_outbox_batch(outbox_ids=[row_id])["sent"] == 1
    assert calls == [("om_original", {"idempotency_key": "question-answer"})] * 2


def test_bot_alone_recovers_stale_replies_in_bounded_batches_without_replaying(warehouse, monkeypatch):
    from jx_alert import bot_service, config
    from jx_alert.notifications.outbox import DELIVERY_UNKNOWN
    from jx_alert.storage import db as storage

    settings = worker.get_settings()
    now = datetime(2026, 11, 22, 2)
    elapsed = now - timedelta(hours=2)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.replace(tzinfo=tz)

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(storage, "SessionLocal", warehouse)
    monkeypatch.setattr(storage, "engine", warehouse.kw["bind"])

    class TickFinished(Exception):
        pass

    def tick():
        def stop(_seconds):
            raise TickFinished
        with monkeypatch.context() as mp:
            mp.setattr(bot_service.time, "sleep", stop)
            with pytest.raises(TickFinished):
                bot_service.run_question_worker()

    calls = []

    class Client:
        def reply_card(self, message_id, value, before_submit, **kwargs):
            before_submit({"operation": "reply"})
            calls.append((message_id, value, kwargs))
            return {"message_id": "om_confirmed_reply"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: Client())

    def add(label, status, claim, *, prefix="group-question:", next_attempt=now):
        om = "om_" + label
        key = f"{prefix}{om}:0"
        payload = {
            "text": "店铺回复", "transport": "app", "fixed_route": True, "card": answer(),
            "delivery": {"role": "group_question", "receive_id": "oc_reports", "open_ids": [],
                         "receive_id_type": "chat_id", "reply_to_message_id": om},
        }
        if status == "SUBMITTING":
            payload["delivery_intent"] = {"operation": "reply", "attempt": 1}
        record = NotificationOutbox(
            idempotency_key=key, chat_id="oc_reports", payload=payload, status=status,
            claimed_at=claim, attempts=int(status != "PENDING"), next_attempt_at=next_attempt, created_at=elapsed,
        )
        with warehouse.begin() as db:
            db.add(record)
            db.flush()
            return record.id, key, om

    held = [add("legacy", "SENDING", None),
            *(add(f"uncertain_{i}", "SUBMITTING", elapsed) for i in range(6))]
    safe = [add(f"safe_{i}", "PREPARING", elapsed if i else None) for i in range(7)]
    untouched = [
        (add("still_preparing", "PREPARING", now), "PREPARING"),
        (add("still_submitting", "SUBMITTING", now), "SUBMITTING"),
        (add("future", "PRE_SEND_RETRY", None, next_attempt=now + timedelta(hours=1)), "PRE_SEND_RETRY"),
        (add("scheduled", "SUBMITTING", elapsed, prefix="scheduled-daily:"), "SUBMITTING"),
        (add("sparkx", "PREPARING", elapsed, prefix="sparkx-page:"), "PREPARING"),
        (add("other", "PENDING", None, prefix="group-question-other:"), "PENDING"),
    ]

    def verify(count):
        with warehouse() as db:
            for (row_id, _, _), status in untouched:
                assert db.get(NotificationOutbox, row_id).status == status
            for index, (row_id, key, _) in enumerate(held):
                row = db.get(NotificationOutbox, row_id)
                if index < count:
                    assert (row.status, row.attempts, row.claimed_at, row.delivered_at) == (
                        DELIVERY_UNKNOWN, 1, None, None)
                    logs = db.execute(select(NotificationLog.status, NotificationLog.message_id)
                                      .where(NotificationLog.dedupe_key == key)).all()
                    assert logs == [(DELIVERY_UNKNOWN, None)]
                else:
                    assert row.status == "SUBMITTING"
            for index, (row_id, _, _) in enumerate(safe):
                row = db.get(NotificationOutbox, row_id)
                assert row.status == ("DELIVERED" if index < count else "PREPARING")
                assert row.attempts == (2 if index < count else 1)
        assert calls == [
            (om, answer(), {"idempotency_key": key}) for _, key, om in safe[:count]
        ]

    # No scheduler and no new question is required to discover old leases.
    tick()
    verify(5)
    tick()
    verify(7)
    tick()
    verify(7)

    # A new reply retains fast-path priority without starving discovery of an
    # old ambiguous lease, and even explicit IDs cannot expand the bot's scope.
    unknown_id, _, _ = add("old_after_idle", "SUBMITTING", elapsed)
    new_id, new_key, new_message = add("new", "PENDING", None)
    other_id = untouched[-1][0][0]
    monkeypatch.setattr(bot_service, "process_one_question", lambda *_: {"outbox_ids": [new_id, other_id]})
    tick()
    assert len(calls) == 8 and calls[-1] == (new_message, answer(), {"idempotency_key": new_key})
    with warehouse() as db:
        assert db.get(NotificationOutbox, unknown_id).status == DELIVERY_UNKNOWN
        assert db.get(NotificationOutbox, new_id).status == "DELIVERED"
        assert db.get(NotificationOutbox, other_id).status == "PENDING"
    tick()
    assert len(calls) == 8
