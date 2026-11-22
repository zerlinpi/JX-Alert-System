"""Offline only: real message encoders, independent DB sessions, in-process provider."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications import worker
from jx_alert.notifications.outbox import DELIVERY_UNKNOWN, NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import NotificationLog


@pytest.fixture
def rig(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    route = tmp_path / "route.json"
    route.write_text(json.dumps({"default_chat_id": "report", "groups": {
        "report": {"chat_id": "oc_fixture"}}}), encoding="utf-8")
    settings = SimpleNamespace(
        app_dry_run=False, feishu_enabled=True, feishu_bot_enabled=True, feishu_transport="app",
        feishu_timeout=1, feishu_app_id="cli_fixture", feishu_app_secret="app-fixture-secret",
        feishu_routing_file=str(route), feishu_daily_report_chat_id="report",
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/private-hook-fixture",
        feishu_sign_secret="fixture-sign-secret", feishu_keyword="fixture", feishu_at_ids=lambda: [],
    )
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    clock = [datetime(2026, 11, 22, 2, 0, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    original_client = httpx.Client
    token = "fixture-bearer-only"
    auth, posts = [], []
    provider = SimpleNamespace(token_ok=True, respond=None)

    def handle(request):
        if request.url.path.endswith("/internal"):
            auth.append(1)
            if not provider.token_ok:
                raise httpx.ReadTimeout("synthetic token timeout")
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": token})
        with sessions() as reader:
            row = reader.scalar(select(NotificationOutbox).where(NotificationOutbox.status == "SUBMITTING"))
            assert row is not None  # Another connection can already see the send fence.
            body = json.loads(request.content)
            intent = row.payload["delivery_intent"]
            if intent["operation"] == "webhook":
                assert "uuid" not in body
                assert intent["endpoint_sha256"] == hashlib.sha256(settings.feishu_webhook_url.encode()).hexdigest()
                body = {k: body[k] for k in ("msg_type", "content")}
            else:
                assert body["uuid"] == intent["provider_uuid"]
            canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            assert hashlib.sha256(canonical).hexdigest() == intent["body_sha256"]
            posts.append((row.id, intent, body))
        return provider.respond(request)

    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))

    def add(kind="create", key="fixture", **row_values):
        payload = {"text": "synthetic-unique-marker", "open_ids": ["ou_fixture"]}
        if kind != "webhook":
            payload.update(transport="app", delivery={"receive_id": "oc_fixture", "receive_id_type": "chat_id",
                                                      "open_ids": ["ou_fixture"]})
        if kind == "reply":
            payload["card"] = {"schema": "2.0", "body": {"elements": [
                {"tag": "markdown", "content": "synthetic-unique-marker"}]}}
            payload["delivery"].update(role="group_question", reply_to_message_id="om_original")
        if kind == "webhook":
            settings.feishu_transport = "webhook"
            settings.feishu_routing_file = str(tmp_path / "absent")
        with sessions.begin() as db:
            row = NotificationOutbox(idempotency_key=key, payload=payload,
                                     next_attempt_at=clock[0].replace(tzinfo=None), **row_values)
            db.add(row)
            db.flush()
            return row.id

    yield SimpleNamespace(add=add, db=sessions, auth=auth, posts=posts, provider=provider, clock=clock,
                          engine=engine, settings=settings)
    engine.dispose()


def success(request):
    return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_receipt"}})


def timeout(request):
    raise httpx.ReadTimeout("remote already accepted; fixture-bearer-only")


@pytest.mark.parametrize("kind", ["create", "reply", "webhook"])
def test_accepted_lost_response_never_replays_even_if_scope_or_body_change(rig, kind):
    row_id = rig.add(kind)
    rig.provider.respond = timeout
    first = worker.process_outbox_batch(outbox_ids=[row_id])
    assert (first["sent"], first["failed"], first["unknown"], first["claimed"]) == (0, 0, 1, 1)
    _, intent, body = rig.posts[0]
    assert intent["operation"] == kind and intent["attempt"] == 1
    if kind == "reply":
        assert intent["reply_to_message_id"] == "om_original"
    assert intent["destination"] == ("default" if kind == "webhook" else "oc_fixture")
    with rig.db.begin() as db:
        row = db.get(NotificationOutbox, row_id)
        log = db.scalar(select(NotificationLog))
        assert row.status == log.status == DELIVERY_UNKNOWN and not row.delivered_at and not log.message_id
        assert row.payload["delivery_intent"] == intent and intent["started_at_utc"] == "2026-11-22T02:00:00Z"
        assert not any(s in str((row.payload, row.last_error, log.error)) for s in (
            "app-fixture-secret", "fixture-sign-secret", "fixture-bearer-only", "private-hook-fixture"))
        row.payload = {**row.payload, "text": "different content", "delivery": {"receive_id": "oc_changed"}}
    rig.provider.respond = success
    for delta in (timedelta(minutes=1), timedelta(hours=2), timedelta(days=2)):
        rig.clock[0] += delta
        assert worker.process_outbox_batch(outbox_ids=[row_id])["claimed"] == 0
    with rig.db() as db:
        assert db.get(NotificationOutbox, row_id).attempts == 1
        assert list(db.scalars(select(NotificationLog.status))) == [DELIVERY_UNKNOWN]
    assert len(rig.posts) == 1


@pytest.mark.parametrize(("http", "body", "expected"), [
    (200, {"code": 0, "data": {}}, DELIVERY_UNKNOWN),
    (200, {"code": 0, "data": {"message_id": "not-an-om-id"}}, DELIVERY_UNKNOWN),
    (200, {"code": 230049}, DELIVERY_UNKNOWN),  # processing != delivered
    (502, {"code": 0, "data": {"message_id": "om_not_a_receipt"}}, DELIVERY_UNKNOWN),
    (500, {"code": 99991663}, DELIVERY_UNKNOWN),  # gateway error cannot prove refusal
    (200, b"not JSON", DELIVERY_UNKNOWN),
    (401, {"code": 99991663}, "REJECTED"),
])
def test_post_response_requires_trustworthy_receipt(rig, http, body, expected):
    row_id = rig.add()
    rig.provider.respond = lambda _: (httpx.Response(http, content=body) if isinstance(body, bytes)
                                      else httpx.Response(http, json=body))
    result = worker.process_outbox_batch()
    assert result["sent"] == 0 and result["unknown"] == int(expected == DELIVERY_UNKNOWN)
    with rig.db() as db:
        assert db.get(NotificationOutbox, row_id).status == expected
        assert db.scalar(select(NotificationLog)).message_id is None
    rig.clock[0] += timedelta(hours=3)
    assert worker.process_outbox_batch()["claimed"] == 0 and len(rig.posts) == 1


def test_pre_submit_auth_timeout_retries_without_intent_or_message_then_succeeds(rig):
    row_id = rig.add()
    rig.provider.token_ok = False
    assert worker.process_outbox_batch()["failed"] == 1
    assert rig.posts == []
    with rig.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "PRE_SEND_RETRY" and "delivery_intent" not in row.payload
    rig.provider.token_ok = True
    rig.provider.respond = success
    rig.clock[0] += timedelta(hours=4)
    assert worker.process_outbox_batch()["sent"] == 1
    assert len(rig.auth) == 2 and len(rig.posts) == 1
    with rig.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "DELIVERED" and row.delivered_at and row.payload["delivery_intent"]["attempt"] == 2
        assert list(db.scalars(select(NotificationLog.status).order_by(NotificationLog.id))) == ["PRE_SEND_RETRY", "DELIVERED"]


def test_receipt_then_local_commit_rollback_holds_without_reposting(rig):
    row_id = rig.add()
    rig.provider.respond = success

    @event.listens_for(rig.db, "before_commit")
    def fail_receipt_commit(db):
        if any(isinstance(item, NotificationLog) and item.status == "DELIVERED" for item in db.new):
            raise RuntimeError("synthetic local receipt commit failure")

    with pytest.raises(RuntimeError, match="synthetic local"):
        worker.process_outbox_batch()
    with rig.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "SUBMITTING" and row.payload["delivery_intent"] and not row.delivered_at
        assert db.scalar(select(NotificationLog)) is None
    assert worker.process_outbox_batch()["unknown"] == 0
    rig.clock[0] += timedelta(hours=2)
    recovered = worker.process_outbox_batch()
    assert recovered["claimed"] == 0 and recovered["unknown"] == 1
    with rig.db() as db:
        row = db.get(NotificationOutbox, row_id)
        log = db.scalar(select(NotificationLog))
        assert row.status == log.status == DELIVERY_UNKNOWN and row.attempts == 1 and not log.message_id
    assert len(rig.posts) == 1


def test_recovery_during_post_never_sends_twice_or_counts_late_receipt_as_committed(rig):
    row_id = rig.add()

    def slow_response(request):
        rig.clock[0] += timedelta(hours=2)
        recovery = worker.process_outbox_batch(outbox_ids=[row_id])
        assert (recovery["unknown"], recovery["claimed"]) == (1, 0)
        return success(request)

    rig.provider.respond = slow_response
    assert worker.process_outbox_batch()["sent"] == 0
    with rig.db() as db:
        assert db.get(NotificationOutbox, row_id).status == DELIVERY_UNKNOWN
        logs = list(db.execute(select(NotificationLog.status, NotificationLog.message_id).order_by(NotificationLog.id)))
        assert logs == [(DELIVERY_UNKNOWN, None), ("STALE_DELIVERED", "om_receipt")]
    assert len(rig.posts) == 1


@pytest.mark.parametrize("state", ["SENDING", "RETRY", "PENDING"])
def test_old_attempt_without_submit_evidence_held_not_replayed(rig, state):
    row_id = rig.add(status=state, attempts=3, claimed_at=None)
    assert worker.process_outbox_batch()["unknown"] == 1
    with rig.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == DELIVERY_UNKNOWN and row.attempts == 3 and "delivery_intent" not in row.payload
    assert worker.process_outbox_batch()["claimed"] == 0 and not rig.posts and not rig.auth


def test_unknown_page_blocks_only_later_pages_in_same_morning_batch(rig):
    first = rig.add(key="batch-a:1")
    second = rig.add(key="batch-a:2")
    other = rig.add(key="batch-b:1")
    with rig.db.begin() as db:
        for oid, batch in ((first, "batch-a:"), (second, "batch-a:"), (other, "batch-b:")):
            row = db.get(NotificationOutbox, oid)
            row.payload = {**row.payload, "morning_batch": batch}
    rig.provider.respond = timeout
    assert worker.process_outbox_batch(outbox_ids=[first])["unknown"] == 1
    rig.provider.respond = success
    rig.clock[0] += timedelta(minutes=2)
    assert worker.process_outbox_batch()["sent"] == 1
    assert [oid for oid, _, _ in rig.posts] == [first, other]
    with rig.db() as db:
        assert db.get(NotificationOutbox, second).status == "PRE_SEND_RETRY"
        assert db.get(NotificationOutbox, second).attempts == 0


def test_crash_after_intent_commit_even_before_message_post_holds_conservatively(rig, monkeypatch):
    row_id = rig.add()
    original = worker._record_submission

    class SimulatedProcessExit(BaseException):
        pass

    def stop_after_commit(*args):
        original(*args)
        raise SimulatedProcessExit

    monkeypatch.setattr(worker, "_record_submission", stop_after_commit)
    with pytest.raises(SimulatedProcessExit):
        worker.process_outbox_batch()
    rig.clock[0] += timedelta(hours=2)
    assert worker.process_outbox_batch()["unknown"] == 1
    with rig.db() as db:
        assert db.get(NotificationOutbox, row_id).status == DELIVERY_UNKNOWN
    assert len(rig.auth) == 1 and not rig.posts


def test_intent_db_failure_before_commit_prevents_post_then_recovers(rig):
    row_id = rig.add()
    rig.provider.respond = success
    failed = []

    @event.listens_for(rig.engine, "before_cursor_execute")
    def fail_first_intent_write(conn, cursor, stmt, params, context, many):
        if not failed and stmt.startswith("UPDATE notification_outbox") and "SUBMITTING" in params:
            failed.append(True)
            raise RuntimeError("synthetic write failed before commit")

    assert worker.process_outbox_batch()["failed"] == 1
    with rig.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "PRE_SEND_RETRY" and "delivery_intent" not in row.payload
    assert failed and not rig.posts
    rig.clock[0] += timedelta(seconds=31)
    assert worker.process_outbox_batch()["sent"] == 1 and len(rig.posts) == 1

@pytest.mark.parametrize(("body", "expected"), [
    ({"code": 0, "StatusCode": 19001}, DELIVERY_UNKNOWN),
    ({"code": 0, "StatusCode": False}, DELIVERY_UNKNOWN),
    ({"code": 0, "StatusCode": "0"}, "DELIVERED"),
])
def test_legacy_webhook_only_accepts_consistent_success_receipts(rig, body, expected):
    row_id = rig.add("webhook")
    rig.provider.respond = lambda _: httpx.Response(200, json=body)
    result = worker.process_outbox_batch()
    assert result["sent"] == int(expected == "DELIVERED")
    assert result["unknown"] == int(expected == DELIVERY_UNKNOWN)
    with rig.db() as db:
        assert db.get(NotificationOutbox, row_id).status == expected
    assert worker.process_outbox_batch()["claimed"] == 0 and len(rig.posts) == 1


@pytest.mark.parametrize(("kind", "raw"), [
    ("create", b'{"code":230049,"code":0,"data":{"message_id":"om_mock"}}'),
    ("reply", b'{"code":0,"data":{"message_id":"bad","message_id":"om_mock"}}'),
    ("webhook", b'{"code":19021,"code":0}'),
])
def test_duplicate_json_members_are_unknown_receipts_and_never_replayed(rig, kind, raw):
    row_id = rig.add(kind)
    rig.provider.respond = lambda _: httpx.Response(200, content=raw)
    result = worker.process_outbox_batch()
    assert (result["sent"], result["unknown"]) == (0, 1)
    with rig.db() as db:
        assert db.get(NotificationOutbox, row_id).status == DELIVERY_UNKNOWN
        assert db.execute(select(NotificationLog.status, NotificationLog.message_id)).all() == [(DELIVERY_UNKNOWN, None)]
    assert worker.process_outbox_batch()["claimed"] == 0 and len(rig.posts) == 1


def test_typed_sku_lost_receipt_preserves_original_intent_and_never_replays(rig, monkeypatch):
    from pathlib import Path
    from jx_alert.notifications.routing import enqueue_application_deliveries
    from jx_alert.storage.models import Store

    Path(rig.settings.feishu_routing_file).write_text(json.dumps({"default_chat_id": "report",
        "groups": {"report": {"chat_id": "oc_fixture", "admin_open_ids": ["ou_admin"]}},
        "stores": {"101": ["ou_owner"]}, "management": {"severities": []}}))
    monkeypatch.setattr(worker, "load_runtime_settings", lambda: rig.settings)
    with rig.db.begin() as db:
        db.add(Store(sid="101", name="mock", status=1))
        result = enqueue_application_deliveries(db, rig.settings, text="fixture", event_key="rule",
            context=dict(store_id="101", entity_id="SKU", scope="101:sales:sku_msku",
                         metrics={}, severity="P1", status="NEW"))
        row = db.get(NotificationOutbox, result["outbox_ids"][0])
        row.next_attempt_at = rig.clock[0].replace(tzinfo=None)
        key, oid = row.idempotency_key, row.id
    rig.provider.respond = timeout
    assert worker.process_outbox_batch()["unknown"] == 1
    rig.clock[0] += timedelta(hours=2)
    assert worker.process_outbox_batch()["claimed"] == 0 and len(rig.posts) == 1
    [(posted_id, intent, body)] = rig.posts
    assert (posted_id, body["receive_id"], intent["provider_uuid"]) == (
        oid, "oc_fixture", hashlib.sha256(key.encode()).hexdigest()[:40])
    with rig.db() as db:
        row = db.get(NotificationOutbox, oid)
        assert row.status == DELIVERY_UNKNOWN and row.payload["delivery_intent"] == intent
        assert row.payload["rule_scope"]["kind"] == "sales_sku" and not row.delivered_at


@pytest.mark.parametrize("status,expected", [
    ("SENDING", DELIVERY_UNKNOWN), ("SUBMITTING", DELIVERY_UNKNOWN), ("RETRY", DELIVERY_UNKNOWN),
    ("DELIVERED", "DELIVERED"), ("REJECTED", "REJECTED"), ("SKIPPED", "SKIPPED"),
    ("DEAD", "DEAD"), ("EXPIRED", "EXPIRED"), (DELIVERY_UNKNOWN, DELIVERY_UNKNOWN),
])
def test_untyped_generic_history_is_not_reclassified_as_a_safe_retry(rig, status, expected):
    oid = rig.add(key="app:" + "3" * 64, status=status, attempts=1)
    assert worker.process_outbox_batch()["sent"] == 0
    assert worker.process_outbox_batch()["claimed"] == 0 and not rig.posts and not rig.auth
    with rig.db() as db:
        row = db.get(NotificationOutbox, oid)
        assert row.status == expected and row.attempts == 1
        assert "rule_scope" not in row.payload
