from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert import config, scheduler
from jx_alert.config import Settings
from jx_alert.notifications import company_executive as notify, worker
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification
from jx_alert.notifications.quiet_policy import json_visible_text
from jx_alert.reporting.company_executive import AD_DIMENSIONS, MARKETPLACE, report_dates
from jx_alert.storage import db as database
from jx_alert.storage.models import DailyMetric, DataCoverage, NotificationLog, Store

DUE = datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc)
LATEST, MATURE = report_dates(DUE)
KEY = notify.PREFIX + "2026-11-23"
APP_ID = "cli_company_synthetic"
APP_SECRET = "synthetic-not-a-real-credential"
APP_IDENTITY = hashlib.sha256(APP_ID.encode("utf-8")).hexdigest()
APP_EVIDENCE = {"app_identity_sha256": APP_IDENTITY}


def _write(path, value):
    Path(path).write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, scheduler_enabled=True, app_dry_run=False, feishu_enabled=True,
        feishu_transport="app", feishu_daily_report_chat_id="reports", feishu_daily_report_enabled=True,
        feishu_app_id=APP_ID, feishu_app_secret=APP_SECRET,
        feishu_company_executive_digest_enabled=True, feishu_company_executive_digest_start_date="2026-11-23",
        hourly_ad_monitor_enabled=True, feishu_quiet_delivery_enabled=True,
        feishu_routing_file=str(tmp_path / "routing.json"), collection_scope_file=str(tmp_path / "scope.json"),
        store_timezones_file=str(tmp_path / "zones.json"), store_controls_file=str(tmp_path / "controls.json"))
    routing = {"default_chat_id": "reports", "groups": {"reports": {"chat_id": "oc_reports"}},
        "stores": {"1": {"chat_id": "reports", "open_ids": ["ou_operator_a"],
                         "owner_display_names": {"ou_operator_a": "运营甲"}},
                   "2": {"chat_id": "reports", "open_ids": ["ou_operator_a"],
                         "owner_display_names": {"ou_operator_a": "运营甲"}},
                   "3": {"chat_id": "reports", "open_ids": ["ou_operator_b"]}},
        "company_executive_digest": {"open_id": "ou_verified_exec", "name": "总负责人", "app_id": APP_ID,
            "generation": "1" * 32, "verified_at": (DUE - timedelta(days=1)).isoformat()}}
    _write(settings.feishu_routing_file, routing)
    _write(settings.collection_scope_file, {"store_ids": ["3", "1", "2"]})
    _write(settings.store_timezones_file,
        {"stores": {str(sid): {"timezone": MARKETPLACE.key} for sid in (1, 2, 3)}})
    engine = create_engine("sqlite:///" + str(tmp_path / "digest.sqlite"))
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "load_runtime_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(config, "load_runtime_settings", lambda: settings)
    clock = SimpleNamespace(now=DUE)

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", TestClock)
    with sessions.begin() as db:
        for data in [
            ("1", "USD", 100, 10, (20, 10), (30, 100, 2, 10)),
            ("2", "USD", 900, 90, (40, 90), (10, 1000, 9, 90)),
            ("3", "EUR", 500, 20, (55, 20), (50, 500, 5, 50)),
        ]:
            sid, currency, sales, orders, ads, mature = data
            db.add(Store(sid=sid, name="Synthetic-" + sid, status=1, timezone=MARKETPLACE.key))
            for dim, entity, count in (("store", sid, 1), ("sku_asin", f"ASIN-{sid}", 1)):
                _coverage(db, sid, LATEST, dim, count)
                _metrics(db, sid, LATEST, dim, entity, "lingxing_sales_report", currency,
                         dict(orders=orders, units=orders * 2, sales=sales))
            for on, fields in [(LATEST, dict(zip(("spend", "clicks"), ads))),
                               (MATURE, dict(zip(("spend", "sales", "orders", "clicks"), mature)))]:
                for dim in AD_DIMENSIONS:
                    _coverage(db, sid, on, dim, int(dim == "campaign_sp"))
                _metrics(db, sid, on, "campaign_sp", "SP-" + sid, "lingxing_ads_sp", currency, fields)
    yield sessions, settings, routing, clock
    engine.dispose()


def _coverage(db, sid, on, dim, count):
    db.add(DataCoverage(store_id=sid, metric_date=on, dimension=dim, row_count=count, status="COMPLETE",
                        completed_at=(DUE - timedelta(hours=1)).replace(tzinfo=None)))


def _metrics(db, sid, on, dim, entity, source, currency, values):
    for key, value in values.items():
        db.add(DailyMetric(store_id=sid, metric_date=on, dimension=dim, entity_id=entity, source=source,
                           metric_name=key, value=value, currency=currency if key in ("sales", "spend") else None))


@pytest.mark.parametrize("due,expected", [
    (datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc), "2026-10-20"),
    (DUE, "2026-11-21"),
    (datetime(2026, 3, 8, 0, 30, tzinfo=timezone.utc), "2026-03-06"),
])
def test_beijing_anchor_chooses_one_exact_completed_marketplace_date(due, expected):
    latest, mature = report_dates(due)
    assert latest.isoformat() == expected
    assert latest - mature == timedelta(days=15)
    assert report_dates(due + timedelta(hours=14)) == (latest, mature)
    with pytest.raises(ValueError, match="timezone-aware"):
        report_dates(due.replace(tzinfo=None))


def test_weighted_and_currency_correct_snapshot_with_explicit_dates(runtime):
    sessions, settings, _, _ = runtime
    with sessions() as db:
        outcome, value = notify.preview_company_digest(db, settings, now=DUE)
        assert db.scalar(select(NotificationOutbox.id)) is None
    assert outcome["date"] == "2026-11-21"
    assert outcome["mature_date"] == "2026-11-06"
    assert outcome["company_orders"] == 120
    usd, eur = (outcome["currencies"][code]["values"] for code in ("USD", "EUR"))
    assert usd == pytest.approx(dict(sales=1000, orders=100, spend=60, acos=40 / 1100,
                                    cvr=11 / 100, ppc=60 / 100, tacos=60 / 1000))
    assert eur == pytest.approx(dict(sales=500, orders=20, spend=55, acos=50 / 500,
                                    cvr=5 / 50, ppc=55 / 20, tacos=55 / 500))
    tables = [e["rows"] for e in value["body"]["elements"] if e["tag"] == "table"]
    assert not any("ACOS" == row["c1"] or row["c1"].startswith("CVR") for row in tables[0])
    assert all(row["c1"] == "ACOS" or row["c1"].startswith("CVR") for row in tables[1])
    plain = json_visible_text(value)
    assert "非唯一订单去重" in plain and "最近经营日的 ACOS 和 CVR 不显示" in plain
    assert outcome["issues"] == [] and outcome["gaps"] == []
    # Each operator row is labelled with the mapped person; currencies stay separate.
    operators = {(row["operator"], row["currency"]): row for row in outcome["operators"]}
    assert set(operators) == {("运营甲", "USD"), ("已映射（姓名待同步）", "EUR")}
    assert operators[("运营甲", "USD")]["values"] == pytest.approx(usd)
    assert operators[("运营甲", "USD")]["store_total"] == 2
    operator_table = tables[2]
    assert operator_table[0]["c0"] == "运营甲 · USD" and operator_table[0]["c1"] == "2/2"
    assert "按运营" in plain and "运营台负责人映射" in plain


@pytest.mark.parametrize("reason", ["bad_currency", "no_coverage", "pre_maturity", "unsupported_source", "zero_denominator",
                                    "preclose", "frozen_future", "invalid_scope_zone", "sparse_asin"])
def test_incomplete_evidence_is_listed_and_never_counted_as_zero(runtime, reason):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        if reason in {"bad_currency", "sparse_asin"}:
            metric = db.scalar(select(DailyMetric).where(DailyMetric.store_id == "2",
                DailyMetric.dimension == "sku_asin", DailyMetric.metric_name == "sales"))
            if reason == "bad_currency":
                metric.currency = None
            else:
                db.delete(metric)
        elif reason in {"no_coverage", "pre_maturity", "preclose", "frozen_future"}:
            row = db.scalar(select(DataCoverage).where(DataCoverage.store_id == "2",
                DataCoverage.metric_date == (MATURE if reason == "pre_maturity" else LATEST),
                DataCoverage.dimension == "campaign_sp"))
            if reason == "no_coverage":
                row.status = "FAILED"
            elif reason == "frozen_future":
                row.completed_at = (DUE + timedelta(seconds=1)).replace(tzinfo=None)
            else:
                row.completed_at = row.completed_at - timedelta(days=2)
        elif reason == "invalid_scope_zone":
            store = db.get(Store, "2")
            store.timezone = "UTC"
        elif reason == "unsupported_source":
            db.scalar(select(DailyMetric).where(DailyMetric.store_id == "2", DailyMetric.metric_date == LATEST,
                DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend")).source = "hourly"
        else:
            for field in db.scalars(select(DailyMetric).where(DailyMetric.store_id.in_(("1", "2")),
                    DailyMetric.metric_date == MATURE, DailyMetric.metric_name.in_(("sales", "clicks")))):
                field.value = 0
    with sessions() as db:
        snapshot, value = notify.preview_company_digest(db, settings, now=DUE)
    plain = json_visible_text(value)
    usd = snapshot["currencies"]["USD"]
    if reason in {"bad_currency", "invalid_scope_zone", "sparse_asin"}:
        # Store 2 cannot be placed in a currency: only store 1 forms the USD total.
        assert [row["sid"] for row in snapshot["issues"]] == ["2"]
        assert snapshot["unconfirmed_currency_stores"] == 1
        assert usd["store_count"] == 1
        assert usd["values"] == pytest.approx(dict(sales=100, orders=10, spend=20, tacos=20 / 100,
                                                   ppc=20 / 10, acos=30 / 100, cvr=2 / 10))
        # An excluded store contributes nothing, not even its separately proven order count.
        assert snapshot["company_orders"] == 30 and snapshot["orders_covered"] == 2
        assert re.search(r"未计入：[^；\n]+（1 家）：Synthetic-2", plain)
    else:
        assert snapshot["company_orders"] == 120 and snapshot["issues"] == []
        expected_gaps = [] if reason == "zero_denominator" else ["2"]
        assert [row["sid"] for row in snapshot["gaps"]] == expected_gaps
        assert bool(re.search(r"部分指标未计入：[^；\n]+（1 家）：Synthetic-2", plain)) is bool(expected_gaps)
        if reason in {"pre_maturity", "zero_denominator"}:
            assert usd["values"]["spend"] == 60
            if reason == "pre_maturity":
                assert usd["values"]["acos"] == pytest.approx(30 / 100)
                assert usd["values"]["cvr"] == pytest.approx(2 / 10)
                assert usd["covered"]["acos"] == 1
            else:
                # Zero denominators are undefined, never displayed as 0 %.
                assert usd["values"]["acos"] is None and usd["values"]["cvr"] is None
                assert usd["covered"]["acos"] == 2
        else:
            # Only store 1 proves spend; store 2 is not counted as zero spend.
            assert usd["values"]["spend"] == 20
            assert usd["values"]["tacos"] == pytest.approx(20 / 100)
            assert usd["covered"]["spend"] == 1 and usd["store_count"] == 2
            assert usd["values"]["sales"] == 1000
        assert snapshot["currencies"]["EUR"]["values"]["spend"] == 55


def test_empty_sales_response_is_named_and_coverage_counts_whole_scope(runtime):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        for row in db.scalars(select(DailyMetric).where(DailyMetric.store_id == "2", DailyMetric.metric_date == LATEST,
                                                       DailyMetric.dimension.in_(("store", "sku_asin")))):
            db.delete(row)
        for row in db.scalars(select(DataCoverage).where(DataCoverage.store_id == "2",
                DataCoverage.metric_date == LATEST, DataCoverage.dimension.in_(("store", "sku_asin")))):
            row.row_count = 0
    with sessions() as db:
        snapshot, value = notify.preview_company_digest(db, settings, now=DUE)
    assert snapshot["issues"] == [{"sid": "2", "name": "Synthetic-2", "operator": "运营甲",
                                   "issue": "当日销量接口无记录（不按 0 计）"}]
    assert snapshot["company_orders"] == 30 and snapshot["orders_covered"] == 2
    first = next(e for e in value["body"]["elements"] if e["tag"] == "table")
    sales = next(row for row in first["rows"] if row["c0"] == "USD" and row["c1"] == "销售额")
    assert sales["c2"] == "100.00 USD" and sales["c3"] == "1/3 店"
    assert "全公司订单量¹：30（覆盖 2/3 店）" in json_visible_text(value)


def test_confirmed_zero_rows_vs_missing_and_currency_mismatch(runtime):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        for row in db.scalars(select(DailyMetric).where(DailyMetric.store_id == "3",
                DailyMetric.metric_date == LATEST, DailyMetric.dimension == "campaign_sp")):
            row.value = 0
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    result = snapshot["currencies"]["EUR"]["values"]
    assert result["spend"] == result["tacos"] == 0
    assert result["ppc"] is None
    with sessions.begin() as db:
        db.scalar(select(DailyMetric).where(DailyMetric.store_id == "2", DailyMetric.metric_date == LATEST,
            DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend")).currency = "EUR"
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    usd = snapshot["currencies"]["USD"]
    # Store 2's spend has the wrong currency: it is excluded, never converted.
    assert usd["values"]["spend"] == 20 and usd["covered"]["spend"] == 1
    assert usd["values"]["tacos"] == pytest.approx(20 / 100)
    assert usd["values"]["acos"] == pytest.approx(40 / 1100)
    assert [row["sid"] for row in snapshot["gaps"]] == ["2"]


@pytest.mark.parametrize("change,expected", [
    ("disabled", "DISABLED"), ("dry_run", "ROUTE_UNVERIFIED"), ("webhook", "ROUTE_UNVERIFIED"),
    ("start_future", "ROUTE_UNVERIFIED"), ("missing_start", "ROUTE_UNVERIFIED"),
    ("report_group", "ROUTE_UNVERIFIED"), ("no_explicit_route", "ROUTE_UNVERIFIED"),
    ("future_verification", "ROUTE_UNVERIFIED"), ("unknown_binding_field", "ROUTE_UNVERIFIED"),
    ("all_paused", "SCOPE_UNVERIFIED"),
])
def test_no_queue_without_explicit_current_private_authorization(runtime, change, expected):
    sessions, settings, routing, _ = runtime
    if change == "disabled":
        settings.feishu_company_executive_digest_enabled = False
    elif change == "dry_run":
        settings.app_dry_run = True
    elif change == "webhook":
        settings.feishu_transport = "webhook"
    elif change == "start_future":
        settings.feishu_company_executive_digest_start_date = "2026-11-24"
    elif change == "missing_start":
        settings.feishu_company_executive_digest_start_date = ""
    elif change == "report_group":
        routing["company_executive_digest"]["open_id"] = "oc_reports"
    elif change == "no_explicit_route":
        del routing["company_executive_digest"]
    elif change == "future_verification":
        routing["company_executive_digest"]["verified_at"] = (DUE + timedelta(days=1)).isoformat()
    elif change == "unknown_binding_field":
        routing["company_executive_digest"]["chat_id"] = "oc_reports"
    else:
        _write(settings.store_controls_file, {"version": 1, "stores": {sid: {
            "collection_enabled": True, "notifications_enabled": False, "updated_at": DUE.isoformat()}
            for sid in ("1", "2", "3")}})
    _write(settings.feishu_routing_file, routing)
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == expected
        assert db.scalar(select(NotificationOutbox.id)) is None


@pytest.mark.parametrize("change", ["paused_store", "collection_paused", "scope_expanded", "approved_subset"])
def test_scope_follows_active_company_stores_and_lists_exclusions(runtime, change):
    sessions, settings, routing, _ = runtime
    expected = ["1", "2", "3"]
    if change in {"paused_store", "collection_paused"}:
        flags = {"collection_enabled": change != "collection_paused",
                 "notifications_enabled": change != "paused_store"}
        _write(settings.store_controls_file, {"version": 1, "stores": {"2": {
            **flags, "updated_at": DUE.isoformat()}}})
        expected = ["1", "3"]
    elif change == "scope_expanded":
        _write(settings.collection_scope_file, {"store_ids": [1, 2, 3, 4]})
        expected = ["1", "2", "3", "4"]
    else:
        routing["company_executive_digest"]["store_ids"] = ["1", "2"]
        _write(settings.feishu_routing_file, routing)
        expected = ["1", "2"]
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == "QUEUED"
        payload = db.scalar(select(NotificationOutbox)).payload
    assert payload["store_ids"] == expected
    plain = json_visible_text(payload["card"])
    if change in {"paused_store", "collection_paused", "approved_subset"}:
        assert "另有 1 家未纳入统计" in plain
    if change == "scope_expanded":
        assert "未计入：尚未同步店铺资料（1 家）：4" in plain


def test_scheduler_registered_for_exact_0830_and_09_lane_preserved(runtime, monkeypatch):
    _, settings, _, _ = runtime
    jobs = {}

    class FakeScheduler:
        def __init__(self, **kwargs):
            pass

        def add_job(self, callback, trigger, *, id, **kwargs):
            jobs[id] = callback, trigger, kwargs

        def start(self):
            pass

    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    for fn in ("init_db", "daily_job", "daily_report_job", "daily_excel_snapshot_job"):
        monkeypatch.setattr(scheduler, fn, lambda: None)
    # The job is always registered and re-reads the runtime switch at each run,
    # so enabling the lane in private configuration needs no scheduler restart.
    settings.feishu_company_executive_digest_enabled = False
    scheduler.main()
    assert jobs["company_executive_digest"][0] is scheduler.company_executive_digest_job
    with database.SessionLocal() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == "DISABLED"
    jobs.clear()
    settings.feishu_company_executive_digest_enabled = True
    scheduler.main()
    assert jobs["company_executive_digest"][0] is scheduler.company_executive_digest_job
    assert jobs["company_executive_digest"][1].get_next_fire_time(None, DUE - timedelta(minutes=1)) == DUE
    assert jobs["company_executive_digest_recovery"][1].interval == timedelta(minutes=1)
    assert jobs["feishu_daily_report"][1].get_next_fire_time(None, DUE) == DUE + timedelta(minutes=30)


def test_one_message_recovers_then_sends_only_private_and_preserves_group_lane(runtime, monkeypatch):
    sessions, settings, _, clock = runtime
    posted = []

    def send(value, *, before_submit, **args):
        before_submit(APP_EVIDENCE)
        posted.append((value, args))
        return {"message_id": "om_synthetic"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: SimpleNamespace(send_card=send, send_text=send))
    scheduler.company_executive_digest_job(now=DUE - timedelta(seconds=1))
    with sessions.begin() as db:
        assert db.scalar(select(NotificationOutbox.id)) is None
        group = enqueue_notification(db, idempotency_key="scheduled-daily:existing", text="09:00 business",
            store_id="1", chat_id="oc_reports", delivery={"role": "daily_snapshot",
                "receive_id": "oc_reports", "receive_id_type": "chat_id", "open_ids": []})
        group.payload = {**group.payload, "scheduled_delivery_date": "2026-11-23", "scheduled_timezone": "Asia/Shanghai"}
        group.next_attempt_at = DUE.replace(tzinfo=None)
    clock.now = DUE + timedelta(minutes=3)
    scheduler.company_executive_digest_job(now=clock.now)  # Recover the missed exact tick.
    scheduler.company_executive_digest_job(now=clock.now)
    assert worker.process_outbox_batch()["sent"] == 1
    assert worker.process_outbox_batch()["sent"] == 0
    assert posted[0][1] == dict(receive_id="ou_verified_exec", receive_id_type="open_id", open_ids=[], idempotency_key=KEY)
    clock.now = DUE + timedelta(minutes=30)
    assert worker.process_outbox_batch()["sent"] == 1
    assert posted[1][1]["receive_id"] == "oc_reports"
    with sessions() as db:
        rows = list(db.scalars(select(NotificationOutbox)))
        assert len(rows) == 2 and all(row.status == "DELIVERED" for row in rows)
        company = next(row for row in rows if row.idempotency_key == KEY)
        assert company.payload["delivery_intent"]["destination_type"] == "open_id"
        assert "app_identity_sha256" not in company.payload["delivery_intent"]
        assert db.scalar(select(NotificationLog.message_id).where(NotificationLog.dedupe_key == KEY)) == "om_synthetic"


def test_frozen_card_from_valid_evidence_survives_later_authorization_change(runtime, monkeypatch):
    sessions, settings, _, _ = runtime
    posted = []

    def send(value, *, before_submit, **args):
        with sessions.begin() as db:
            db.get(Store, "2").status = 2
        before_submit(APP_EVIDENCE)
        posted.append(args["receive_id"])
        return {"message_id": "om_synthetic"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: SimpleNamespace(send_card=send))
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == "QUEUED"
    assert worker.process_outbox_batch()["sent"] == 1
    assert posted == ["ou_verified_exec"]


def test_owner_status_projection_has_no_business_numbers(runtime):
    _, settings, _, _ = runtime
    status = notify.digest_status(settings)
    assert status["enabled"] is True and status["configuration_valid"] is True
    assert status["recipient"] == {"id": "ou_verified_exec", "name": "总负责人",
                                   "verified_at": (DUE - timedelta(days=1)).isoformat()}
    assert status["store_count"] == 3 and status["excluded_store_count"] == 0
    assert APP_ID not in json.dumps(status)


@pytest.mark.parametrize("change", ["generation", "recipient", "scope", "pause", "deadline", "disabled"])
def test_final_submission_fence_rechecks_permission_after_token_step(runtime, monkeypatch, change):
    sessions, settings, routing, clock = runtime
    posted = []

    def send(value, *, before_submit, **args):
        if change == "generation":
            routing["company_executive_digest"]["generation"] = "2" * 32
        elif change == "recipient":
            routing["company_executive_digest"]["open_id"] = "ou_other"
        elif change == "scope":
            _write(settings.collection_scope_file, {"store_ids": ["1", "2"]})
        elif change == "pause":
            _write(settings.store_controls_file, {"version": 1, "stores": {"1": {
                "collection_enabled": True, "notifications_enabled": False, "updated_at": DUE.isoformat()}}})
        elif change == "deadline":
            clock.now = DUE + timedelta(minutes=10)
        elif change == "disabled":
            settings.feishu_company_executive_digest_enabled = False
        _write(settings.feishu_routing_file, routing)
        before_submit(APP_EVIDENCE)
        posted.append(value)
        return {"message_id": "should_not_exist"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: SimpleNamespace(send_card=send))
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == "QUEUED"
    assert worker.process_outbox_batch()["sent"] == 0
    assert posted == []
    with sessions() as db:
        row = db.scalar(select(NotificationOutbox))
        assert row.status == "SKIPPED" and row.chat_id == "ou_verified_exec"
        assert row.payload.get("delivery_intent") is None


def test_expired_or_ambiguous_digest_is_not_replayed(runtime, monkeypatch):
    sessions, settings, _, clock = runtime
    scheduler.company_executive_digest_job(now=DUE + timedelta(minutes=10))
    with sessions.begin() as db:
        assert db.scalar(select(NotificationOutbox.id)) is None
        assert notify.queue_company_digest(db, settings, now=DUE) == "QUEUED"
    posts = []

    def send(value, *, before_submit, **args):
        before_submit(APP_EVIDENCE)
        posts.append(args["idempotency_key"])
        raise TimeoutError

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: SimpleNamespace(send_card=send))
    assert worker.process_outbox_batch()["unknown"] == 1
    assert worker.process_outbox_batch()["unknown"] == 0
    clock.now = DUE + timedelta(minutes=10)
    scheduler.company_executive_digest_job(now=clock.now)
    assert worker.process_outbox_batch()["sent"] == 0
    assert posts == [KEY]
    with sessions() as db:
        assert db.scalar(select(NotificationOutbox.status)) == "DELIVERY_UNKNOWN"


def test_view_capable_formats_do_not_enter_click_cvr(runtime):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        sb = db.scalar(select(DataCoverage).where(DataCoverage.store_id == "1",
            DataCoverage.metric_date == MATURE, DataCoverage.dimension == "campaign_sb"))
        sb.row_count = 1
        _metrics(db, "1", MATURE, "campaign_sb", "SB-1", "lingxing_ads_sb", "USD",
                 {"spend": 100, "sales": 300, "orders": 100, "clicks": 2})
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    values = snapshot["currencies"]["USD"]["values"]
    assert values["acos"] == pytest.approx(140 / 1400)
    assert values["cvr"] == pytest.approx(11 / 100)
    with sessions.begin() as db:
        db.scalar(select(DataCoverage).where(DataCoverage.store_id == "1",
            DataCoverage.metric_date == MATURE, DataCoverage.dimension == "campaign_sb")).status = "FAILED"
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    usd = snapshot["currencies"]["USD"]
    # Store 1's mature SB report failed: its ACOS is excluded rather than partially summed.
    assert usd["values"]["acos"] == pytest.approx(10 / 1000) and usd["covered"]["acos"] == 1
    assert usd["values"]["cvr"] == pytest.approx(11 / 100)
    assert snapshot["gaps"][0]["sid"] == "1" and "SB" in snapshot["gaps"][0]["gaps"][0]


def test_mature_captures_after_lookback_but_inside_reporting_lag_are_unavailable(runtime):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        db.scalar(select(DataCoverage).where(DataCoverage.store_id == "1",
            DataCoverage.metric_date == MATURE, DataCoverage.dimension == "campaign_sp")).completed_at = (
                datetime.combine(MATURE + timedelta(days=15), time(6), MARKETPLACE)
                .astimezone(timezone.utc).replace(tzinfo=None))
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    usd = snapshot["currencies"]["USD"]
    # Store 1's capture is inside the reporting cushion: only store 2 proves maturity.
    assert usd["values"]["acos"] == pytest.approx(10 / 1000) and usd["covered"]["acos"] == 1
    assert usd["values"]["cvr"] == pytest.approx(9 / 90) and usd["covered"]["cvr"] == 1



def test_prior_beijing_1830_refresh_can_prove_exact_mature_cohort(runtime):
    sessions, settings, _, _ = runtime
    # Nov 22 18:30 Beijing is after the Nov 6 cohort's 14 full days
    # plus 12 hours. The otherwise newer Nov 7 cohort misses that cushion.
    prior_broad_refresh = DUE - timedelta(hours=14)
    with sessions.begin() as db:
        for row in db.scalars(select(DataCoverage).where(DataCoverage.metric_date == MATURE)):
            row.completed_at = prior_broad_refresh.replace(tzinfo=None)
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    assert snapshot["mature_date"] == "2026-11-06"
    assert snapshot["currencies"]["USD"]["values"]["acos"] == pytest.approx(40 / 1100)
    assert snapshot["currencies"]["USD"]["values"]["cvr"] == pytest.approx(11 / 100)


def test_recovery_reuses_original_frozen_card_when_data_changes(runtime):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == "QUEUED"
        stored = db.scalar(select(NotificationOutbox)).payload
    with sessions.begin() as db:
        for row in db.scalars(select(DailyMetric).where(DailyMetric.metric_date == LATEST,
                                                       DailyMetric.metric_name == "sales")):
            row.value *= 2
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE + timedelta(minutes=3)) == "ALREADY_QUEUED"
        assert db.scalar(select(NotificationOutbox)).payload == stored


@pytest.mark.parametrize("file,field", [("route", "open_id"), ("scope", "store_ids"), ("zone", "timezone"),
                                       ("credential", "app_id")])
def test_duplicate_privacy_inputs_fail_closed(runtime, tmp_path, file, field):
    sessions, settings, routing, _ = runtime
    if file == "route":
        path = Path(settings.feishu_routing_file)
        content = json.dumps(routing).replace('"open_id":', '"open_id":"ou_other","open_id":', 1)
    elif file == "scope":
        path = Path(settings.collection_scope_file)
        content = '{"store_ids":[1,2],"store_ids":[1,2,3]}'
    elif file == "zone":
        path = Path(settings.store_timezones_file)
        content = path.read_text(encoding="utf-8").replace('"timezone":', '"timezone":"UTC","timezone":', 1)
    else:
        path = tmp_path / "credentials.json"
        settings.feishu_app_credentials_file = str(path)
        settings.feishu_app_id = ""
        from pydantic import SecretStr
        settings.feishu_app_secret = SecretStr("")
        content = '{"app_id":"cli_other","app_id":"' + APP_ID + '","app_secret":"' + APP_SECRET + '"}'
    assert content.count('"' + field + '"') >= 2
    path.write_text(content, encoding="utf-8")
    with sessions.begin() as db:
        if file in {"route", "credential"}:
            assert notify.queue_company_digest(db, settings, now=DUE) == "ROUTE_UNVERIFIED"
        else:
            with pytest.raises(ValueError, match="duplicate JSON field"):
                notify.queue_company_digest(db, settings, now=DUE)
        assert db.scalar(select(NotificationOutbox.id)) is None


@pytest.mark.parametrize("mode", [
    "stable", "configured_app_rotates", "wrong_cached_adapter", "duplicate_post_config",
    "duplicate_store_pause",
])
def test_actual_adapter_and_current_config_must_match_verified_app_at_post(runtime, monkeypatch, mode):
    sessions, settings, routing, _ = runtime
    calls = []
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == "QUEUED"
        frozen = db.scalar(select(NotificationOutbox)).payload
        assert APP_ID not in json.dumps(frozen) and APP_SECRET not in json.dumps(frozen)
    transport_app = "cli_wrong_cached_adapter" if mode == "wrong_cached_adapter" else APP_ID
    client = FeishuAppClient(transport_app, APP_SECRET)
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: client)

    def handle(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == FeishuAppClient._TOKEN_PATH:
            assert body["app_id"] == transport_app
            if mode == "configured_app_rotates":
                settings.feishu_app_id = "cli_rotated_after_token"
            if mode == "duplicate_post_config":
                raw = json.dumps(routing).replace('"open_id":', '"open_id":"ou_other","open_id":', 1)
                Path(settings.feishu_routing_file).write_text(raw, encoding="utf-8")
            if mode == "duplicate_store_pause":
                common = {"collection_enabled": True, "updated_at": DUE.isoformat()}
                paused = json.dumps({**common, "notifications_enabled": False})
                enabled = json.dumps({**common, "notifications_enabled": True})
                Path(settings.store_controls_file).write_text(
                    '{"version":1,"stores":{"1":' + paused + ',"1":' + enabled + '}}',
                    encoding="utf-8",
                )
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-token", "expire": 7200})
        assert request.url.path == FeishuAppClient._MESSAGE_PATH
        assert request.url.params["receive_id_type"] == "open_id"
        assert body["receive_id"] == "ou_verified_exec"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_synthetic_app"}})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    sent = worker.process_outbox_batch()["sent"]
    if mode == "stable":
        assert sent == 1
        assert len(calls) == 2
    else:
        assert sent == 0
        assert len(calls) == 1 and calls[0][0] == FeishuAppClient._TOKEN_PATH
    with sessions() as db:
        row = db.scalar(select(NotificationOutbox))
        assert row.status == ("DELIVERED" if mode == "stable" else "SKIPPED")
        payload = json.dumps(row.payload)
        assert APP_ID not in payload and APP_SECRET not in payload and "synthetic-token" not in payload
        assert "app_identity_sha256" not in (row.payload.get("delivery_intent") or {})
