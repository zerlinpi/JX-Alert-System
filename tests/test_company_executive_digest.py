from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
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
        "company_executive_digest": {"open_id": "ou_verified_exec", "app_id": APP_ID, "generation": "1" * 32,
            "verified_at": (DUE - timedelta(days=1)).isoformat(), "store_ids": [1, 2, 3]}}
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


@pytest.mark.parametrize("reason", ["bad_currency", "no_coverage", "pre_maturity", "unsupported_source", "zero_denominator",
                                    "preclose", "frozen_future", "invalid_scope_zone", "sparse_asin"])
def test_incomplete_evidence_does_not_create_partial_total_or_zero(runtime, reason):
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
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    if reason in {"bad_currency", "invalid_scope_zone", "sparse_asin"}:
        assert snapshot["currencies"] == {}
        assert snapshot["unconfirmed_currency_stores"] == 1
    else:
        assert snapshot["company_orders"] == 120
        usd = snapshot["currencies"]["USD"]
        if reason in {"pre_maturity", "zero_denominator"}:
            assert usd["values"]["acos"] is None and usd["values"]["cvr"] is None
            assert usd["values"]["spend"] == 60
            assert usd["covered"]["acos"] == (1 if reason == "pre_maturity" else 2)
        else:
            assert usd["values"]["spend"] is None and usd["values"]["tacos"] is None
            assert usd["covered"]["spend"] == 1
        assert snapshot["currencies"]["EUR"]["values"]["spend"] == 55


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
    assert snapshot["currencies"]["USD"]["values"]["tacos"] is None
    assert snapshot["currencies"]["USD"]["values"]["acos"] == pytest.approx(40 / 1100)


@pytest.mark.parametrize("change,expected", [
    ("disabled", "DISABLED"), ("dry_run", "ROUTE_UNVERIFIED"), ("webhook", "ROUTE_UNVERIFIED"),
    ("start_future", "ROUTE_UNVERIFIED"), ("missing_start", "ROUTE_UNVERIFIED"),
    ("report_group", "ROUTE_UNVERIFIED"), ("no_explicit_route", "ROUTE_UNVERIFIED"),
    ("visible_subset", "ROUTE_UNVERIFIED"), ("future_verification", "ROUTE_UNVERIFIED"),
    ("scope_expanded", "ROUTE_UNVERIFIED"), ("paused_store", "SCOPE_UNVERIFIED"),
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
    elif change == "visible_subset":
        routing["company_executive_digest"]["store_ids"] = ["1", "2"]
    elif change == "future_verification":
        routing["company_executive_digest"]["verified_at"] = (DUE + timedelta(days=1)).isoformat()
    elif change == "scope_expanded":
        _write(settings.collection_scope_file, {"store_ids": [1, 2, 3, 4]})
    else:
        _write(settings.store_controls_file, {"version": 1, "stores": {"2": {
            "collection_enabled": True, "notifications_enabled": False, "updated_at": DUE.isoformat()}}})
    _write(settings.feishu_routing_file, routing)
    with sessions.begin() as db:
        assert notify.queue_company_digest(db, settings, now=DUE) == expected
        assert db.scalar(select(NotificationOutbox.id)) is None


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
    settings.feishu_company_executive_digest_enabled = False
    scheduler.main()
    assert "company_executive_digest" not in jobs
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


@pytest.mark.parametrize("change", ["generation", "recipient", "scope", "authorization", "pause", "deadline", "disabled"])
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
        elif change == "authorization":
            with sessions.begin() as db:
                db.get(Store, "2").status = 2
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
    values = snapshot["currencies"]["USD"]["values"]
    assert values["acos"] is None
    assert values["cvr"] == pytest.approx(11 / 100)


def test_mature_captures_after_lookback_but_inside_reporting_lag_are_unavailable(runtime):
    sessions, settings, _, _ = runtime
    with sessions.begin() as db:
        db.scalar(select(DataCoverage).where(DataCoverage.store_id == "1",
            DataCoverage.metric_date == MATURE, DataCoverage.dimension == "campaign_sp")).completed_at = (
                datetime.combine(MATURE + timedelta(days=15), time(6), MARKETPLACE)
                .astimezone(timezone.utc).replace(tzinfo=None))
    with sessions() as db:
        snapshot, _ = notify.preview_company_digest(db, settings, now=DUE)
    assert snapshot["currencies"]["USD"]["values"]["acos"] is None
    assert snapshot["currencies"]["USD"]["values"]["cvr"] is None



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
