from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo, available_timezones

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert import operations
from jx_alert.chat_analysis import prepare_review, submit_review
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.readiness import analysis_plan
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store


CLOCK = datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    scope, zones, routing = [tmp_path / name for name in ["scope.json", "zones.json", "routing.json"]]
    scope.write_text(json.dumps({"store_ids": [1, 2, 3]}))
    zones.write_text(json.dumps({"stores": {
        "1": {"timezone": "America/Los_Angeles", "currency_code": "USD"},
        "2": {"timezone": None, "currency_code": ""},
        "3": {"timezone": "", "currency_code": ""},
    }}))
    routing.write_text(json.dumps({"default_chat_id": "test", "groups": {"test": {"chat_id": "oc_test"}},
        "stores": {"2": {"open_ids": ["ou_operator"]}}}))
    settings = SimpleNamespace(collection_scope_file=str(scope), store_timezones_file=str(zones),
        allow_unconfigured_timezone_history=True, app_dry_run=False, sp_collection_enabled=False,
        sp_refresh_days=7, ad_attribution_refresh_days=22, report_dir=str(tmp_path), raw_dir=str(tmp_path),
        feishu_routing_file=str(routing), feishu_transport="app", feishu_daily_report_chat_id="test",
        feishu_enabled=True, feishu_daily_report_enabled=True, feishu_daily_report_start_date="",
        feishu_daily_report_cron="0 8 * * *")
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    from jx_alert import config
    from jx_alert.storage import db
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setattr(db, "engine", engine)
    yield settings, sessions, zones
    engine.dispose()


def inventory():
    return {"data": [{"sid": 1, "name": "Known", "status": 1},
                     {"sid": 2, "name": "Unknown timezone", "status": 1},
                     {"sid": 3, "name": "Authorization failed", "status": 2}]}


def sync(settings, sessions):
    with sessions() as db:
        result = operations.sync_scoped_store_inventory(db, settings, inventory())
        db.commit()
        return result


def test_default_missing_timezone_is_blocked_but_explicit_history_basis_is_safe_in_every_zone(setup):
    settings, _, zones = setup
    strict = operations.store_report_plan(inventory()["data"], zones, now=CLOCK)
    assert [x["state"] for x in strict] == ["READY", "WAITING_TIMEZONE", "AUTHORIZATION_FAILED"]
    plan = operations.store_report_plan(inventory()["data"], zones, now=CLOCK, allow_unknown_timezone_history=True)
    unknown = plan[1]
    assert unknown["timezone"] is None and unknown["timezone_confirmed"] is False
    assert unknown["intraday_enabled"] is False
    assert unknown["date_basis"] == "global_complete_unknown_timezone"
    assert unknown["report_date"] == "2026-11-11"
    for zone in available_timezones():
        assert date.fromisoformat(unknown["report_date"]) < CLOCK.astimezone(ZoneInfo(zone)).date()


def test_inventory_sync_keeps_all_stores_visible_and_never_invents_timezone_or_currency(setup):
    settings, sessions, _ = setup
    sync(settings, sessions)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Store)) == 3
        assert db.get(Store, "1").timezone == "America/Los_Angeles"
        assert db.get(Store, "2").timezone is None and db.get(Store, "2").currency_code is None
        plan = analysis_plan(db, settings, now=CLOCK)
        assert plan["enabled_store_count"] == 3 and plan["collection_ready_count"] == 2
        assert {r["store_id"]: r["state"] for r in plan["stores"]} == {
            "1": "NO_DATA", "2": "NO_DATA", "3": "AUTHORIZATION_FAILED"}


def test_unknown_timezone_history_supports_count_analysis_but_empty_data_is_not_normal(setup):
    settings, sessions, _ = setup
    sync(settings, sessions)
    with sessions() as db:
        review = prepare_review(db, settings, "2", now=CLOCK)
        assert review["timezone"] is None and review["date_basis"] == "global_complete_unknown_timezone"
        result = submit_review(db, settings, review["review_id"], [], now=CLOCK)
        assert result["status"] == "INSUFFICIENT_DATA" and result["outbox_ids"] == []
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert db.get(Store, "2").timezone is None


def test_complete_count_baseline_can_be_normal_without_assuming_currency(setup):
    settings, sessions, _ = setup
    sync(settings, sessions)
    with sessions() as db:
        for i in range(8):
            day = date(2026, 11, 11) - timedelta(days=i)
            db.add(DailyMetric(store_id="2", dimension="store", entity_id="2", metric_date=day,
                metric_name="orders", value=0))
            db.add(DataCoverage(store_id="2", dimension="store", metric_date=day, status="COMPLETE"))
        db.flush()
        review = prepare_review(db, settings, "2", now=CLOCK)
        assert review["data_sufficient"]
        assert submit_review(db, settings, review["review_id"], [], now=CLOCK)["status"] == "NO_ANOMALY"
        plan = analysis_plan(db, settings, now=CLOCK)
        item = next(x for x in plan["stores"] if x["store_id"] == "2")
        assert item["analysis_ready"] and not item["amount_analysis_ready"]


def test_currency_unknown_or_conflicting_evidence_cannot_generate_money_alert(setup):
    settings, sessions, _ = setup
    sync(settings, sessions)
    with sessions() as db:
        for day, currency in [(date(2026, 11, 10), "USD"), (date(2026, 11, 11), "CAD")]:
            db.add(DailyMetric(store_id="2", dimension="store", entity_id="2", metric_date=day,
                metric_name="sales", value=100, currency=currency))
            db.add(DataCoverage(store_id="2", dimension="store", metric_date=day, status="COMPLETE"))
        db.flush()
        review = prepare_review(db, settings, "2", now=CLOCK)
        proposed = [{"topic": "销售额异常", "summary": "销售额变化需核实", "action": "核实销售情况",
                     "evidence_ids": [r["id"] for r in review["facts"]]}]
        with pytest.raises(ValueError, match="currency"):
            submit_review(db, settings, review["review_id"], proposed, now=CLOCK)
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_one_store_failure_does_not_stop_other_stores_and_retry_reuses_checkpoints(setup, monkeypatch):
    settings, sessions, _ = setup
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert.lingxing import pipeline
    from jx_alert.storage import db as database, job_lock, raw_archive
    from jx_alert.analysis import evaluator
    from jx_alert.reporting import excel
    monkeypatch.setattr(job_lock, "database_job_lock", lambda engine: nullcontext())
    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(LingxingClient, "from_settings", lambda: SimpleNamespace(get_stores=inventory))
    calls = []

    def collect(client, db, report_dates, **kwargs):
        sid = next(iter(report_dates))
        calls.append(sid)
        if sid == "1":
            raise RuntimeError("provider failure must not be copied into result")
        return {"status": "SUCCEEDED"}

    monkeypatch.setattr(pipeline, "collect_daily_snapshot", collect)
    monkeypatch.setattr(evaluator, "evaluate_daily_anomalies", lambda *args: {})
    monkeypatch.setattr(excel, "export_daily_reports", lambda *args, **kwargs: [])
    monkeypatch.setattr(raw_archive, "archive_raw_snapshots", lambda *args: {})
    result = operations.run_collection()
    assert calls == ["1", "2"] and result["status"] == "PARTIAL"
    assert result["completed_store_ids"] == ["2"] and result["failed_store_ids"] == ["1"]
    assert result["stores"]["1"] == {"status": "FAILED", "error_type": "RuntimeError"}
    assert result["blocked_stores"][0]["store_id"] == "3"
    again = operations.run_collection(completed_store_dates=result["completed_store_dates"])
    assert calls == ["1", "2", "1"]
    assert again["stores"]["2"]["status"] == "REUSED_COMPLETED_CHECKPOINT"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Store)) == 3
    operations.run_collection(completed_store_dates={"2": "2000-01-01"})
    assert calls[-2:] == ["1", "2"], "A checkpoint for another report date cannot suppress fresh collection"


def test_unknown_timezone_daily_report_keeps_explicit_observed_counts(setup):
    settings, sessions, _ = setup
    sync(settings, sessions)
    with sessions() as db:
        db.add(DailyMetric(store_id="2", dimension="store", entity_id="2", metric_date=date(2026, 11, 11),
            metric_name="orders", value=7))
        db.add(DataCoverage(store_id="2", dimension="store", metric_date=date(2026, 11, 11), status="COMPLETE"))
        db.commit()
    result = operations.queue_scheduled_daily_reports(now=CLOCK)
    with sessions() as db:
        row = db.scalar(select(NotificationOutbox).where(NotificationOutbox.payload["store_id"].as_string() == "2"))
        assert row.id in result["outbox_ids"]
        assert "订单 7" in row.payload["text"]


def test_complete_sales_observation_sets_currency_and_conflict_never_overwrites(setup):
    settings, sessions, _ = setup
    from jx_alert.lingxing.sales_collector import collect_sales_for_day

    class Client:
        def sales_daily(self, payload):
            return {"total": 1, "data": [{"r_date": payload["event_date"], "seller_sku": "sku", "asin": "asin",
                "currency_code": "CAD", "map_value": 1}]}

    stores = {"data": [{"sid": 2, "name": "Unknown timezone", "status": 1}]}
    with sessions() as db:
        collect_sales_for_day(Client(), db, "2026-11-11", stores_response=stores)
        assert db.get(Store, "2").currency_code == "CAD" and db.get(Store, "2").timezone is None
        db.get(Store, "2").currency_code = "USD"
        db.commit()
        with pytest.raises(RuntimeError, match="currency conflicts"):
            collect_sales_for_day(Client(), db, "2026-11-12", stores_response=stores)
        db.rollback()
        assert db.get(Store, "2").currency_code == "USD"
        assert db.scalar(select(func.count()).select_from(DailyMetric).where(DailyMetric.metric_date == date(2026, 11, 12))) == 0
