"""Real SQL acceptance tests for the disposable MariaDB 11 service in CI.

Never use the application's DATABASE_URL. Opt in only on an isolated container
network with the dedicated service and fresh, empty schema defined in the acceptance workflow.
The service lifetime handles cleanup; no existing tables are dropped or truncated.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.orm import sessionmaker

from jx_alert.notifications import feishu_app, worker
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock
from jx_alert.storage.models import NotificationLog

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
            assert version.startswith("11.") and "MariaDB" in version, version
            assert schema == "jx_ci_acceptance" and not inspect(conn).get_table_names(), (
                "refusing to test with a nonempty or unexpected schema"
            )
        for model in (NotificationOutbox, NotificationLog):
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


def test_expired_worker_cannot_send_after_same_second_takeover(engine, monkeypatch, tmp_path):
    sessions = sessionmaker(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app",
                               feishu_timeout=1, feishu_routing_file=str(tmp_path / "empty.json"))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sends = []

    class SyntheticSink:
        def send_text(self, message, **kwargs):
            sends.append((message, kwargs["idempotency_key"]))
            return {"message_id": "fixture-receipt"}

    monkeypatch.setattr(feishu_app.FeishuAppClient, "from_settings", lambda _: SyntheticSink())
    monkeypatch.setattr(worker, "FeishuClient", lambda **_: pytest.fail("unexpected webhook"))

    # Freeze time and age only the first lease in the DB. When worker B reclaims
    # it, DATETIME(0) matches worker A's saved timestamp; only attempts fences A.
    instant = datetime.now(timezone.utc).replace(microsecond=123456)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", FixedClock)
    stamp = instant.replace(tzinfo=None, microsecond=0)
    key = "ci-expired-claim"
    with sessions.begin() as db:
        row = NotificationOutbox(idempotency_key=key, next_attempt_at=stamp - timedelta(minutes=1),
                                 payload={"text": "fixture", "transport": "app", "delivery": {
                                     "receive_id": "oc_fixture", "receive_id_type": "chat_id"}})
        db.add(row)
        db.flush()
        row_id = row.id

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
            # B holds SENDING and has not sent. Let A reach the real SQL fence
            # now, not after B's terminal status could reject A by itself.
            resume_first.set()
            first.result(timeout=15)
        return real_policy(db, *args)

    monkeypatch.setattr(worker, "_proactive_delivery_reason", interleave)

    def run_worker():
        return worker.process_outbox_batch(outbox_ids=[row_id])

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(run_worker)
        try:
            assert first_paused.wait(timeout=15)
            assert run_worker()["claimed"] == 0  # a live lease is not eligible
            with sessions.begin() as db:
                changed = db.execute(update(NotificationOutbox).where(
                    NotificationOutbox.id == row_id, NotificationOutbox.status == "SENDING",
                    NotificationOutbox.attempts == 1, NotificationOutbox.claimed_at == stamp,
                ).values(claimed_at=stamp - timedelta(days=1)))
                assert changed.rowcount == 1
            successor = run_worker()
        finally:
            resume_first.set()
        stale = first.result(timeout=15)

    assert [item[1:] for item in observations] == [("SENDING", 1, stamp), ("SENDING", 2, stamp)]
    assert observations[0][0] != observations[1][0]
    assert (stale["claimed"], stale["sent"], stale["failed"]) == (1, 0, 0)
    assert (successor["claimed"], successor["sent"], successor["failed"]) == (1, 1, 0)
    assert sends == [("fixture", key)]
    assert run_worker()["claimed"] == 0
    with sessions() as db:
        row = db.get(NotificationOutbox, row_id)
        assert (row.status, row.attempts, row.claimed_at) == ("DELIVERED", 2, None)
        logs = db.execute(select(NotificationLog.status, NotificationLog.message_id)
                          .where(NotificationLog.dedupe_key == key)).all()
        assert logs == [("DELIVERED", "fixture-receipt")]
