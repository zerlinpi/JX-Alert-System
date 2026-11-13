from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from threading import Event
from types import SimpleNamespace

from apscheduler.executors.pool import ThreadPoolExecutor as SchedulerExecutor
from apscheduler.schedulers.background import BackgroundScheduler
import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from jx_alert import operations, scheduler
from jx_alert.notifications import daily_report, worker
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage import db as database
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, Store, SyncJob


DEADLINE = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)  # Beijing 08:00, LA previous-day 16:00


@pytest.fixture
def setup(monkeypatch, tmp_path):
    scope = tmp_path / "scope.json"
    zones = tmp_path / "timezones.json"
    routing = tmp_path / "routing.json"
    scope.write_text(json.dumps({"store_ids": [5174]}), encoding="utf-8")
    zones.write_text(json.dumps({"stores": {
        "5174": {"timezone": "America/Los_Angeles", "currency_code": "USD"},
        "5175": {"timezone": "Asia/Tokyo", "currency_code": "JPY"},
    }}), encoding="utf-8")
    routing.write_text(json.dumps({"default_chat_id": "reports", "groups": {
        "reports": {"chat_id": "oc_reports"},
    }}), encoding="utf-8")
    settings = SimpleNamespace(
        app_dry_run=False, feishu_enabled=True, feishu_daily_report_enabled=True,
        feishu_daily_report_start_date="", feishu_daily_report_chat_id="reports",
        feishu_transport="app", feishu_routing_file=str(routing),
        collection_scope_file=str(scope), store_timezones_file=str(zones),
        scheduler_enabled=True, app_timezone="UTC", daily_sync_cron="0 7 * * *",
        feishu_daily_report_cron="0 8 * * *", report_dir=str(tmp_path / "reports"),
        raw_dir=str(tmp_path / "raw"), sp_refresh_days=7, ad_attribution_refresh_days=22,
    )
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        db.add(Store(sid="5174", name="Selected", status=1, timezone="America/Los_Angeles",
                     currency_code="USD"))
        db.add(Store(sid="5175", name="Unselected", status=1, timezone="Asia/Tokyo",
                     currency_code="JPY"))
        db.commit()
    import jx_alert.config as config
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(database, "engine", engine)
    yield settings, sessions, scope, zones
    engine.dispose()


def capture_enqueues(monkeypatch):
    calls = []

    def enqueue(db, settings, **kwargs):
        calls.append(kwargs)
        return {"queued": len(kwargs["store_dates"]), "skipped": False, "outbox_ids": [100]}

    monkeypatch.setattr(daily_report, "enqueue_daily_snapshots", enqueue)
    return calls


def test_before_beijing_deadline_never_reads_warehouse_or_calls_provider(setup, monkeypatch):
    calls = capture_enqueues(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("The pre-deadline gate must not connect to the warehouse or a provider")

    monkeypatch.setattr(database, "SessionLocal", forbidden)
    from jx_alert.lingxing.client import LingxingClient
    monkeypatch.setattr(LingxingClient, "from_settings", forbidden)
    result = operations.queue_scheduled_daily_reports(now=DEADLINE - timedelta(microseconds=1))
    assert result["status"] == "WAITING" and result["outbox_ids"] == []
    assert result["not_before"] == "2026-11-13T08:00:00+08:00"
    assert calls == []


def test_first_delivery_date_prevents_old_day_catchup_and_allows_next_deadline(setup, monkeypatch):
    settings, _, _, _ = setup
    settings.feishu_daily_report_start_date = "2026-11-14"
    calls = capture_enqueues(monkeypatch)
    assert operations.queue_scheduled_daily_reports(now=DEADLINE)["status"] == "WAITING"
    assert calls == []
    result = operations.queue_scheduled_daily_reports(now=DEADLINE + timedelta(days=1))
    assert result["delivery_date"] == "2026-11-14"
    assert calls[0]["delivery_date"].isoformat() == "2026-11-14"


@pytest.mark.parametrize("hour", [7, 9])
def test_queue_gate_uses_the_same_configured_beijing_cron_as_notification_helper(setup, monkeypatch, hour):
    settings, _, _, _ = setup
    settings.feishu_daily_report_cron = f"0 {hour} * * *"
    calls = capture_enqueues(monkeypatch)
    due = DEADLINE + timedelta(hours=hour - 8)
    waiting = operations.queue_scheduled_daily_reports(now=due - timedelta(microseconds=1))
    assert waiting["status"] == "WAITING"
    assert waiting["not_before"] == f"2026-11-13T{hour:02d}:00:00+08:00"
    assert calls == []
    assert operations.queue_scheduled_daily_reports(now=due)["status"] == "QUEUED"
    assert len(calls) == 1


@pytest.mark.parametrize("start", ["", "2026-11-14"])
def test_no_cron_fire_on_current_or_future_start_date_fails_closed(setup, monkeypatch, start):
    settings, _, _, _ = setup
    settings.feishu_daily_report_cron = "0 8 * * mon"
    settings.feishu_daily_report_start_date = start
    calls = capture_enqueues(monkeypatch)
    with pytest.raises(ValueError, match="no delivery window"):
        operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert calls == []


def test_deadline_uses_latest_complete_marketplace_date_only_for_explicit_scope(setup, monkeypatch):
    calls = capture_enqueues(monkeypatch)
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.storage import job_lock

    def forbidden(*args, **kwargs):
        pytest.fail("Scheduled reports must not acquire a collection lock, initialize DB or call Lingxing")

    monkeypatch.setattr(LingxingClient, "from_settings", forbidden)
    monkeypatch.setattr(job_lock, "database_job_lock", forbidden)
    monkeypatch.setattr(database, "init_db", forbidden)
    result = operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert result["status"] == "QUEUED"
    assert result["report_dates"] == {"5174": "2026-11-11"}
    assert calls[0]["store_dates"] == {"5174": "2026-11-11"}
    assert calls[0]["delivery_date"].isoformat() == "2026-11-13"
    assert calls[0]["now"] == DEADLINE
    assert [row.sid for row in calls[0]["store_records"]] == ["5174"]
    assert calls[0]["store_warnings"] == {}


def test_multiple_scoped_stores_keep_each_marketplace_date(setup, monkeypatch):
    _, _, scope, _ = setup
    scope.write_text(json.dumps({"store_ids": [5174, 5175]}), encoding="utf-8")
    calls = capture_enqueues(monkeypatch)
    result = operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert result["report_dates"] == {"5174": "2026-11-11", "5175": "2026-11-12"}
    assert calls[0]["store_dates"] == result["report_dates"]


@pytest.mark.parametrize("failure", ["missing_scope", "invalid_scope", "missing_timezone", "invalid_timezone"])
def test_scope_and_confirmed_timezone_prerequisites_fail_closed(setup, monkeypatch, failure):
    _, _, scope, zones = setup
    calls = capture_enqueues(monkeypatch)
    if failure == "missing_scope":
        scope.unlink()
    elif failure == "invalid_scope":
        scope.write_text(json.dumps({"store_ids": [True]}), encoding="utf-8")
    elif failure == "missing_timezone":
        scope.write_text(json.dumps({"store_ids": [9999]}), encoding="utf-8")
    else:
        zones.write_text(json.dumps({"stores": {"5174": {"timezone": "Not/A_Confirmed_Zone"}}}), encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert calls == []


def test_missing_database_store_still_queues_unknown_without_creating_store(setup, monkeypatch):
    _, sessions, scope, zones = setup
    scope.write_text(json.dumps({"store_ids": [5174, 5176]}), encoding="utf-8")
    config = json.loads(zones.read_text(encoding="utf-8"))
    config["stores"]["5176"] = {"timezone": "Asia/Tokyo", "currency_code": "JPY"}
    zones.write_text(json.dumps(config), encoding="utf-8")
    calls = capture_enqueues(monkeypatch)
    result = operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert result["report_dates"] == {"5174": "2026-11-11", "5176": "2026-11-12"}
    temporary = next(store for store in calls[0]["store_records"] if store.sid == "5176")
    assert inspect(temporary).transient and temporary.timezone == "Asia/Tokyo"
    assert calls[0]["store_warnings"]["5176"] == "尚未完成店铺数据同步"
    with sessions() as db:
        assert db.get(Store, "5176") is None
        assert db.scalar(select(func.count()).select_from(Store)) == 2


def test_changed_timezone_and_inactive_store_only_warn_without_business_writes(setup, monkeypatch):
    _, sessions, _, _ = setup
    with sessions() as db:
        store = db.get(Store, "5174")
        store.timezone, store.status = "UTC", 0
        db.commit()
    calls = capture_enqueues(monkeypatch)
    result = operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert result["report_dates"] == {"5174": "2026-11-11"}
    temporary = calls[0]["store_records"][0]
    assert inspect(temporary).transient and temporary.timezone == "America/Los_Angeles"
    assert "时区配置已更新" in calls[0]["store_warnings"]["5174"]
    assert "非正常同步状态" in calls[0]["store_warnings"]["5174"]
    with sessions() as db:
        assert db.get(Store, "5174").timezone == "UTC"


def test_missing_database_store_queues_a_real_unknown_card_without_creating_store(setup):
    _, sessions, scope, _ = setup
    # Store 5175 is present in the confirmed timezone file, but not synchronized.
    scope.write_text(json.dumps({"store_ids": [5174, 5175]}), encoding="utf-8")
    with sessions() as db:
        db.delete(db.get(Store, "5175"))
        db.commit()
    result = operations.queue_scheduled_daily_reports(now=DEADLINE)
    assert len(result["outbox_ids"]) == 2
    with sessions() as db:
        missing = db.scalar(select(NotificationOutbox).where(
            NotificationOutbox.payload["store_id"].as_string() == "5175",
        ))
        assert missing is not None and "未知" in missing.payload["text"]
        assert "尚未完成店铺数据同步" in json.dumps(missing.payload, ensure_ascii=False)
        assert db.get(Store, "5175") is None
        assert db.scalar(select(func.count()).select_from(Store)) == 1


@pytest.mark.parametrize("changes,status", [
    ({"app_dry_run": True}, "DRY_RUN"),
    ({"feishu_enabled": False}, "DISABLED"),
    ({"feishu_daily_report_enabled": False}, "DISABLED"),
])
def test_disabled_or_dry_run_never_queues(setup, monkeypatch, changes, status):
    settings, _, _, _ = setup
    for key, value in changes.items():
        setattr(settings, key, value)
    calls = capture_enqueues(monkeypatch)
    assert operations.queue_scheduled_daily_reports(now=DEADLINE)["status"] == status
    assert calls == []


def test_naive_clock_is_rejected_before_scheduling(setup, monkeypatch):
    calls = capture_enqueues(monkeypatch)
    with pytest.raises(ValueError, match="timezone-aware"):
        operations.queue_scheduled_daily_reports(now=DEADLINE.replace(tzinfo=None))
    assert calls == []


def test_report_sender_targets_only_these_ids_in_bounded_batches(monkeypatch):
    monkeypatch.setattr(operations, "queue_scheduled_daily_reports", lambda: {
        "status": "QUEUED", "outbox_ids": [101, 102, 103, 104, 105, 106, 106],
    })
    calls = []
    monkeypatch.setattr(worker, "process_outbox_batch", lambda **kwargs: (
        calls.append(kwargs) or {"sent": len(kwargs["outbox_ids"]), "claimed": len(kwargs["outbox_ids"])},
    )[0])
    scheduler.daily_report_job()
    assert calls == [
        {"batch_size": 5, "outbox_ids": [101, 102, 103, 104, 105]},
        {"batch_size": 1, "outbox_ids": [106]},
    ]


def test_predeadline_report_job_does_not_drain_other_outbox_messages(monkeypatch):
    monkeypatch.setattr(operations, "queue_scheduled_daily_reports", lambda: {
        "status": "WAITING", "outbox_ids": [],
    })
    monkeypatch.setattr(worker, "process_outbox_batch", lambda **kwargs: pytest.fail("No report is due"))
    scheduler.daily_report_job()


def test_separate_scheduler_executor_runs_deadline_report_during_long_collection(monkeypatch):
    entered, release, reported = Event(), Event(), Event()

    def long_collection():
        entered.set()
        assert release.wait(5), "Test must release collection"
        return {"status": "COMPLETE"}

    monkeypatch.setattr(operations, "run_collection", long_collection)
    monkeypatch.setattr(operations, "queue_scheduled_daily_reports", lambda: {
        "status": "QUEUED", "outbox_ids": [901],
    })
    monkeypatch.setattr(worker, "process_outbox_batch", lambda **kwargs: (
        reported.set() or {"claimed": 1, "sent": 1},
    )[0])
    running = BackgroundScheduler(timezone="Asia/Shanghai", executors={
        "default": SchedulerExecutor(max_workers=1),
        "daily_reports": SchedulerExecutor(max_workers=1),
    })
    try:
        running.start()
        running.add_job(scheduler.daily_job, "date", executor="default")
        assert entered.wait(3)
        running.add_job(scheduler.daily_report_job, "date", executor="daily_reports")
        assert reported.wait(3), "The deadline report must not wait for the collection executor"
        assert not release.is_set()
    finally:
        release.set()
        running.shutdown(wait=True)


def test_scheduler_cron_is_fixed_beijing_0800_and_startup_attempt_is_separate(setup, monkeypatch):
    captured = {}
    starts = []

    class FakeScheduler:
        def __init__(self, **kwargs):
            captured["settings"] = kwargs
            captured["jobs"] = {}

        def add_job(self, callback, trigger, **kwargs):
            captured["jobs"][kwargs["id"]] = (callback, trigger, kwargs)

        def start(self):
            starts.append("scheduler")

    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    monkeypatch.setattr(scheduler, "daily_report_job", lambda: starts.append("report"))
    scheduler.main()
    _, trigger, kwargs = captured["jobs"]["feishu_daily_report"]
    assert str(trigger.timezone) == "Asia/Shanghai"
    assert trigger.get_next_fire_time(None, DEADLINE - timedelta(seconds=1)).astimezone(timezone.utc) == DEADLINE
    assert kwargs["executor"] == "daily_reports" and kwargs["max_instances"] == 1
    recovery_callback, recovery_trigger, recovery = captured["jobs"]["feishu_daily_report_recovery"]
    assert recovery_callback is captured["jobs"]["feishu_daily_report"][0]
    assert recovery_trigger.interval.total_seconds() == 60
    assert recovery["executor"] == "daily_reports" and recovery["max_instances"] == 1
    assert recovery["coalesce"] is True
    _, collection_trigger, _ = captured["jobs"]["daily_pipeline"]
    assert str(collection_trigger.timezone) == "Asia/Shanghai"
    preparation = DEADLINE - timedelta(hours=1)
    assert collection_trigger.get_next_fire_time(None, preparation - timedelta(seconds=1)).astimezone(timezone.utc) == preparation
    assert "daily_reports" in captured["settings"]["executors"]
    assert starts == ["report", "scheduler"]


def test_collection_completes_without_enqueuing_a_daily_report(setup, monkeypatch):
    settings, _, _, _ = setup
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.lingxing import pipeline
    from jx_alert.storage import job_lock, raw_archive, store_config
    from jx_alert.analysis import evaluator
    from jx_alert.reporting import excel

    monkeypatch.setattr(job_lock, "database_job_lock", lambda engine: nullcontext())
    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(LingxingClient, "from_settings", lambda: SimpleNamespace(
        get_stores=lambda: {"data": [{"sid": 5174, "status": 1}]},
    ))
    monkeypatch.setattr(store_config, "build_store_report_dates", lambda *args: {"5174": "2026-11-11"})
    monkeypatch.setattr(store_config, "sync_store_config", lambda *args: 0)
    monkeypatch.setattr(operations, "collect_sp_targets", lambda *args, **kwargs: {"status": "DISABLED"})
    monkeypatch.setattr(pipeline, "collect_daily_snapshot", lambda *args, **kwargs: {"status": "SUCCEEDED"})
    monkeypatch.setattr(evaluator, "evaluate_daily_anomalies", lambda *args: {})
    monkeypatch.setattr(excel, "export_daily_reports", lambda *args, **kwargs: [])
    monkeypatch.setattr(raw_archive, "archive_raw_snapshots", lambda *args: {})
    monkeypatch.setattr(daily_report, "enqueue_daily_snapshots", lambda *args, **kwargs: pytest.fail(
        "Collection completion must not independently enqueue a daily report",
    ))
    result = operations.run_collection()
    assert result["status"] == "COMPLETE"
    assert result["daily_notifications"] == {"queued": 0, "skipped": True, "reason": "scheduled_report"}


def test_missing_data_report_still_queues_once_and_repeated_delivery_does_not_resend(setup, monkeypatch):
    settings, sessions, _, _ = setup
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.notifications.feishu_app import FeishuAppClient
    monkeypatch.setattr(LingxingClient, "from_settings", lambda: pytest.fail("No Lingxing calls"))
    with sessions() as db:
        db.add(SyncJob(job_type="daily_collection", status="FAILED",
                       metadata_json={"error_type": "UnavailableSource"}))
        db.commit()
    first = operations.queue_scheduled_daily_reports(now=DEADLINE)
    second = operations.queue_scheduled_daily_reports(now=DEADLINE + timedelta(minutes=1))
    assert first["outbox_ids"] == second["outbox_ids"]
    with sessions() as db:
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert len(jobs) == 1
        assert "未知" in jobs[0].payload["text"]
        assert jobs[0].payload["store_id"] == "5174"
    sent = []

    def send(*args, **kwargs):
        sent.append(kwargs)
        return {"message_id": "test-message"}

    class WorkerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = DEADLINE + timedelta(minutes=1)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    settings.feishu_timeout = 1
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "datetime", WorkerClock)
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda settings: SimpleNamespace(
        send_card=send, send_text=send,
    ))
    first_send = worker.process_outbox_batch(outbox_ids=first["outbox_ids"])
    assert first_send["sent"] == 1
    delivered = operations.queue_scheduled_daily_reports(now=DEADLINE + timedelta(minutes=2))
    assert delivered["outbox_ids"] == first["outbox_ids"]
    assert worker.process_outbox_batch(outbox_ids=delivered["outbox_ids"])["claimed"] == 0
    assert len(sent) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        assert db.scalar(select(NotificationOutbox)).status == "DELIVERED"
        # Late collection can update business facts, but cannot resend today's
        # already-delivered scheduled report under a new snapshot hash.
        db.add(DailyMetric(store_id="5174", metric_date=datetime(2026, 11, 11).date(),
                           dimension="store", entity_id="5174", metric_name="orders", value=42))
        db.commit()
    late = operations.queue_scheduled_daily_reports(now=DEADLINE + timedelta(minutes=3))
    assert worker.process_outbox_batch(outbox_ids=late["outbox_ids"])["claimed"] == 0
    assert len(sent) == 1


def test_minutely_report_recovery_retries_a_transient_queue_failure_without_restart(setup, monkeypatch):
    _, sessions, _, _ = setup
    real_queue = operations.queue_scheduled_daily_reports
    attempts, sent = [], []

    def open_report_session():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OperationalError("test queue query", {}, Exception("temporary unavailability"))
        return sessions()

    def deliver(*, batch_size, outbox_ids):
        with sessions() as db:
            rows = db.scalars(select(NotificationOutbox).where(
                NotificationOutbox.id.in_(outbox_ids), NotificationOutbox.status == "PENDING",
            )).all()
            for row in rows:
                sent.append(row.id)
                row.status = "DELIVERED"
            db.commit()
        return {"claimed": len(rows), "sent": len(rows)}

    monkeypatch.setattr(database, "SessionLocal", open_report_session)
    monkeypatch.setattr(operations, "queue_scheduled_daily_reports", lambda: real_queue(
        now=DEADLINE + timedelta(minutes=len(attempts)),
    ))
    monkeypatch.setattr(worker, "process_outbox_batch", deliver)
    scheduler.daily_report_job()  # The 08:00 attempt fails before any outbox row exists.
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    assert sent == []
    scheduler.daily_report_job()  # The independent next-minute recovery succeeds.
    scheduler.daily_report_job()  # Later recovery ticks retain the same delivery identity.
    assert attempts == [1, 2, 3]
    assert len(sent) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        assert db.scalar(select(NotificationOutbox)).status == "DELIVERED"
