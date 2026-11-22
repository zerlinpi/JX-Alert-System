"""Real SQL acceptance tests for the disposable MariaDB 11 service in CI.

Never use the application's DATABASE_URL. Opt in only on an isolated container
network with the dedicated service and fresh, empty schema defined in the acceptance workflow.
The service lifetime handles cleanup; no existing tables are dropped or truncated.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
from mcp.types import CallToolResult, TextContent
import pytest
from sqlalchemy import create_engine, event, inspect, select, text, update
from sqlalchemy.orm import sessionmaker

from jx_alert import sparkx_feishu
from jx_alert.notifications import worker
from jx_alert.notifications.outbox import DELIVERY_UNKNOWN, NotificationOutbox
from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock
from jx_alert.storage.models import NotificationLog, Store

pytestmark = pytest.mark.skipif(
    os.environ.get("JX_MARIADB_ACCEPTANCE") != "1", reason="requires disposable CI MariaDB 11"
)

# Intentionally no override: the worker recovers stale claims database-wide.
_CI_DATABASE = "mysql+pymysql://jx_ci:ci-test-only@jx-ci-mariadb/jx_ci_acceptance"


@pytest.fixture(scope="module")
def engine():
    db = create_engine(_CI_DATABASE, connect_args={"connect_timeout": 5, "read_timeout": 15})
    try:
        with db.connect() as conn:
            version, schema = conn.execute(text("SELECT VERSION(), DATABASE()")).one()
            assert version.startswith("11.4.") and "MariaDB" in version, version
            assert schema == "jx_ci_acceptance" and not inspect(conn).get_table_names(), (
                "refusing to test with a nonempty or unexpected schema"
            )
        print(f"Isolated acceptance database: MariaDB {version}, schema={schema}", flush=True)
        for model in (NotificationOutbox, NotificationLog, Store):
            model.__table__.create(db)
        yield db
    finally:
        db.dispose()


def test_job_lock_fences_another_database_session_across_commits(engine):
    # Keep both physical connections pooled and alive. A disconnect would release
    # a MariaDB advisory lock implicitly and could conceal a missing RELEASE_LOCK.
    owner = create_engine(_CI_DATABASE, pool_size=1, max_overflow=0)
    try:
        with database_job_lock(owner, "ci_acceptance", pin_connection=True) as locked:
            assert not locked.in_transaction()
            owner_id = locked.scalar(text("SELECT CONNECTION_ID()"))
            locked.commit()
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT CONNECTION_ID()")) != owner_id
            with pytest.raises(JobAlreadyRunning):
                with database_job_lock(engine, "ci_acceptance"):
                    pytest.fail("competing session acquired the owner's lock")
        with database_job_lock(engine, "ci_acceptance", pin_connection=True) as successor:
            assert successor.scalar(text("SELECT CONNECTION_ID()")) != owner_id
            with owner.connect() as conn:
                assert conn.scalar(text("SELECT CONNECTION_ID()")) == owner_id
    finally:
        owner.dispose()


@pytest.fixture
def transport(engine, monkeypatch, tmp_path):
    """Run the production Feishu client/encoders with no provider network."""
    sessions = sessionmaker(engine)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app",
        feishu_timeout=1, feishu_routing_file=str(tmp_path / "routing.json"),
        feishu_app_id="cli_ci_fixture", feishu_app_secret="ci-fixture-only",
        feishu_quiet_delivery_enabled=False, store_controls_file=str(tmp_path / "no-pauses.json"))
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)

    instant = [datetime.now(timezone.utc).replace(microsecond=123456)]

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant[0].astimezone(tz) if tz is not None else instant[0].replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", FixedClock)
    auths, posts = [], []
    receipt = SimpleNamespace(lose=False)
    real_client = httpx.Client

    def provider(req):
        assert req.method == "POST" and req.url.host == "open.feishu.cn"
        body = json.loads(req.content)
        if req.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert body["app_id"] == "cli_ci_fixture"
            auths.append(req.url.path)
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "fixture"})
        assert req.url.path == "/open-apis/im/v1/messages"
        assert req.url.params["receive_id_type"] == "chat_id" and req.headers["Authorization"] == "Bearer fixture"
        # A second real MariaDB session must already see the committed send intent.
        with sessions() as db:
            row = db.scalars(select(NotificationOutbox).where(NotificationOutbox.status == "SUBMITTING")).one()
            intent = row.payload["delivery_intent"]
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            assert intent["body_sha256"] == hashlib.sha256(encoded).hexdigest()
            assert intent["destination"] == body["receive_id"] == "oc_fixture"
            assert intent["provider_uuid"] == body["uuid"] == hashlib.sha256(
                row.idempotency_key.encode()).hexdigest()[:40]
            assert intent["attempt"] == row.attempts and intent["operation"] == "create"
            posts.append((row.id, row.attempts, body))
        if receipt.lose:
            raise httpx.ReadTimeout("synthetic accepted message; response dropped", request=req)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_ci_receipt"}})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(provider), **kw))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: pytest.fail("unexpected live SparkX HTTP"))
    monkeypatch.setattr(worker, "FeishuClient", lambda **_: pytest.fail("unexpected webhook"))
    return SimpleNamespace(db=sessions, settings=settings, instant=instant, auths=auths, posts=posts, receipt=receipt)


def _insert(transport, key):
    stamp = transport.instant[0].replace(tzinfo=None, microsecond=0)
    with transport.db.begin() as db:
        row = NotificationOutbox(idempotency_key=key, next_attempt_at=stamp - timedelta(minutes=1),
                                 payload={"text": "fixture", "transport": "app", "delivery": {
                                     "receive_id": "oc_fixture", "receive_id_type": "chat_id"}})
        db.add(row)
        db.flush()
        return row.id


def _logs(db, key):
    return db.execute(select(NotificationLog.status, NotificationLog.message_id)
                      .where(NotificationLog.dedupe_key == key).order_by(NotificationLog.id)).all()


def test_expired_worker_cannot_post_after_same_second_takeover(transport, monkeypatch):
    sessions = transport.db
    # Age the original lease in the database, but keep process clocks frozen:
    # MariaDB DATETIME(0) then gives both PREPARING claims the same timestamp.
    stamp = transport.instant[0].replace(tzinfo=None, microsecond=0)
    key = "ci-expired-claim"
    row_id = _insert(transport, key)
    first_paused, resume_first = Event(), Event()
    observations = []
    real_policy = worker._proactive_delivery_reason

    def interleave(db, *args):
        row = db.get(NotificationOutbox, row_id)
        observations.append((db.scalar(text("SELECT CONNECTION_ID()")), row.status,
                             row.attempts, row.claimed_at))
        if row.attempts == 1:
            first_paused.set()
            if not resume_first.wait(timeout=15):
                pytest.fail("successor did not reach its pre-send check")
        else:
            # Let A authenticate and reach the actual message POST fence while
            # B still holds PREPARING. Status/timestamp alone cannot stop A.
            resume_first.set()
            stale = first.result(timeout=15)
            assert (stale["claimed"], stale["sent"], stale["failed"], stale["unknown"]) == (1, 0, 0, 0)
            assert len(transport.auths) == 1 and not transport.posts
        return real_policy(db, *args)

    monkeypatch.setattr(worker, "_proactive_delivery_reason", interleave)

    def run_worker():
        return worker.process_outbox_batch(outbox_ids=[row_id])

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(run_worker)
        try:
            assert first_paused.wait(timeout=15)
            assert run_worker()["claimed"] == 0
            with sessions.begin() as db:
                changed = db.execute(update(NotificationOutbox).where(
                    NotificationOutbox.id == row_id, NotificationOutbox.status == "PREPARING",
                    NotificationOutbox.attempts == 1, NotificationOutbox.claimed_at == stamp,
                ).values(claimed_at=stamp - timedelta(days=1)))
                assert changed.rowcount == 1
            successor = run_worker()
        finally:
            resume_first.set()
        first.result(timeout=15)

    assert [item[1:] for item in observations] == [("PREPARING", 1, stamp), ("PREPARING", 2, stamp)]
    assert observations[0][0] != observations[1][0]
    assert (successor["claimed"], successor["sent"], successor["failed"], successor["unknown"]) == (1, 1, 0, 0)
    assert len(transport.posts) == 1 and transport.posts[0][:2] == (row_id, 2)
    assert json.loads(transport.posts[0][2]["content"])["text"] == "fixture"
    assert run_worker()["claimed"] == 0
    with sessions() as db:
        row = db.get(NotificationOutbox, row_id)
        assert (row.status, row.attempts, row.claimed_at) == ("DELIVERED", 2, None)
        assert row.delivered_at and _logs(db, key) == [("DELIVERED", "om_ci_receipt")]


def test_sparkx_scope_and_wire_body_survive_lost_response_without_replay(transport, monkeypatch):
    cfg = transport.settings
    cfg.sparkx_feishu_enabled = True
    cfg.sparkx_profile_ids = str(profile := 9007199254740993)
    cfg.sparkx_ai_token = SimpleNamespace(get_secret_value=lambda: "ci-fixture-only")
    cfg.feishu_daily_report_chat_id = "acceptance"

    Path(cfg.feishu_routing_file).write_text(json.dumps({
        "default_chat_id": "acceptance",
        "groups": {"acceptance": {"chat_id": "oc_fixture", "verified_member_open_ids": ["ou_ci"]}},
        "stores": {"101": {"chat_id": "acceptance"}},
        "sparkx_profile_routes": {str(profile): {"store_id": "101", "chat_id": "oc_fixture",
                                                 "currency": "USD", "generation": "a" * 32}},
    }), encoding="utf-8")
    with transport.db.begin() as db:
        db.add(Store(sid="101", name="CI-only", status=1, currency_code="USD"))
    scope = {"profileIds": [profile], "factEntity": "campaign", "dateStart": "2026-10-01",
             "dateEnd": "2026-10-01", "metrics": ["Spend"], "pageSize": 1, "userContext": "fixture"}
    remote = SimpleNamespace(spend=0)
    call_names = []

    async def mock_sparkx(name, arguments, *, token):
        call_names.append(name)
        assert token == "ci-fixture-only"
        if name == "get_user_authorized_context":
            assert arguments == {}
            return CallToolResult(content=[], structuredContent={"profiles": [{"profileId": profile}]})
        assert name == "get_ads_perf" and arguments == scope
        return CallToolResult(content=[TextContent(type="text", text="Not bound to this report's scope")],
            structuredContent={"isError": False, "toolName": name,
                "data": {"rows": [{"date": "20261001", "Spend": remote.spend}], "rowCount": 1},
                "meta": {"page": 1, "pageSize": 1, "hasNextPage": False, "effectiveProfileIds": [profile],
                         "currency": "USD", "hint": "Only this scoped page."}})

    monkeypatch.setattr(sparkx_feishu, "call_sparkx", mock_sparkx)

    def enqueue():
        return asyncio.run(sparkx_feishu.prepare(cfg, deepcopy(scope), enqueue=True, session_factory=transport.db))

    item = enqueue()
    assert item["status"] == "PENDING" and not transport.auths and not transport.posts
    row_id = item["outbox_id"]
    with transport.db() as db:
        row = db.get(NotificationOutbox, row_id)
        planned, key = row.payload["card"], row.idempotency_key
        assert "Not bound" not in str(planned)
        # Derive the worker clock from committed DATETIME(0) values; fixture
        # setup may otherwise span a real wall-clock second and race due time.
        transport.instant[0] = max(row.next_attempt_at, row.created_at).replace(tzinfo=timezone.utc) + timedelta(seconds=2)
    transport.receipt.lose = True
    first = worker.process_outbox_batch(outbox_ids=[row_id])
    assert (first["sent"], first["unknown"], first["failed"]) == (0, 1, 0)
    assert len(transport.posts) == 1
    body = transport.posts[0][2]
    assert body["msg_type"] == "interactive" and json.loads(body["content"]) == planned
    assert "<at " not in body["content"]

    transport.instant[0] += timedelta(hours=2)
    transport.receipt.lose = False
    remote.spend = 500
    assert enqueue() == {**item, "status": DELIVERY_UNKNOWN}
    assert call_names == ["get_user_authorized_context", "get_ads_perf"] * 2
    assert worker.process_outbox_batch(outbox_ids=[row_id])["claimed"] == 0
    assert len(transport.posts) == len(transport.auths) == 1
    with transport.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == DELIVERY_UNKNOWN and row.attempts == 1 and row.delivered_at is None
        assert row.payload["card"] == planned and _logs(db, key) == [(DELIVERY_UNKNOWN, None)]


def test_provider_receipt_plus_local_commit_failure_holds_after_lease(transport):
    key = "ci-receipt-uncommitted"
    row_id = _insert(transport, key)

    @event.listens_for(transport.db, "before_commit")
    def fail_final_commit(db):
        if any(isinstance(log, NotificationLog) and log.dedupe_key == key and log.status == "DELIVERED"
               for log in db.new):
            raise RuntimeError("fixture receipt transaction aborted before COMMIT")

    def dispatch():
        return worker.process_outbox_batch(outbox_ids=[row_id])

    with pytest.raises(RuntimeError, match="fixture receipt transaction aborted"):
        dispatch()
    with transport.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == "SUBMITTING" and row.delivered_at is None and _logs(db, key) == []
    assert len(transport.posts) == 1 and dispatch()["claimed"] == 0
    transport.instant[0] += timedelta(hours=2)
    recovered = dispatch()
    assert (recovered["claimed"], recovered["unknown"], recovered["sent"]) == (0, 1, 0)
    assert dispatch()["claimed"] == 0 and len(transport.posts) == 1
    with transport.db() as db:
        row = db.get(NotificationOutbox, row_id)
        assert row.status == DELIVERY_UNKNOWN and row.attempts == 1 and row.delivered_at is None
        assert _logs(db, key) == [(DELIVERY_UNKNOWN, None)]
