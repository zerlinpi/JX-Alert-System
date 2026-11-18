"""API-only prerequisites cannot hide independently collected MCP business facts."""
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert import config, operations
from jx_alert.lingxing import business_dashboard, pipeline, sales_collector, source_gateway
from jx_alert.lingxing.client import LingxingCollectionPaused, LingxingError
from jx_alert.notifications.business_daily import DIMENSION
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage import db as storage
from jx_alert.storage.models import DailyMetric, NotificationLog, Store


DAY = "2026-11-16"
PRIOR = "2026-11-15"
BUSINESS = {"volume": 25, "netAmount": 4089.75, "platformIncome": 3208.16,
            "grossProfit": 387.22, "grossRate": .1218,
            "totalAdsCostRate": -.2158, "totalRefundsRate": -.0816}


class Reader:
    def stores(self):
        return [{"id": 5174}], {"code": 1}

    def profiles(self):
        return [], {"code": 1}

    def sales(self, sid, day, grain):
        return [], {"code": 1}

    def campaign_ads(self, sid, day, kind):
        return [], {"code": 1}


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.db"))
    storage.Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "engine", engine)
    monkeypatch.setattr(storage, "SessionLocal", sessions)
    monkeypatch.setattr(storage, "init_db", lambda: None)
    with sessions() as db:
        db.add(Store(sid="5174", name="Geteen US", status=1, has_ads_setting=1,
                     timezone="America/Los_Angeles", currency_code="USD"))
        db.commit()

    scope, zones, targets = (tmp_path / name for name in ("scope.json", "zones.json", "targets.json"))
    scope.write_text(json.dumps({"store_ids": [5174]}), encoding="utf-8")
    zones.write_text(json.dumps({"stores": {"5174": {
        "timezone": "America/Los_Angeles", "currency_code": "USD"}}}), encoding="utf-8")
    targets.write_text(json.dumps({"targets": [{"sid": 5174, "campaign_id": 99}]}), encoding="utf-8")
    settings = config.Settings(_env_file=None, app_dry_run=False,
        collection_scope_file=str(scope), store_timezones_file=str(zones),
        store_controls_file=str(tmp_path / "switches.json"),
        sp_collection_enabled=True, sp_validation_targets_file=str(targets), sp_refresh_days=1,
        ad_attribution_refresh_days=1, sb_sd_detail_reports_enabled=False,
        lingxing_performance_enabled=False, excel_archive_enabled=False,
        lingxing_business_credentials_file=str(tmp_path / "absent-business.json"),
        lingxing_sources_file=str(tmp_path / "absent-sources.json"),
        raw_dir=str(tmp_path / "raw"), report_dir=str(tmp_path / "reports"),
        feishu_enabled=True, feishu_daily_report_enabled=True)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    plan = operations.store_report_plan
    monkeypatch.setattr(operations, "store_report_plan", lambda *args, **kwargs: plan(
        *args, **kwargs, now=datetime(2026, 11, 18, 12, tzinfo=timezone.utc)))
    source = source_gateway.CollectionSources(None, settings, reader=Reader())
    monkeypatch.setattr(source_gateway, "create_collection_client", lambda settings: source)

    from jx_alert.analysis import evaluator
    from jx_alert.reporting import excel
    monkeypatch.setattr(evaluator, "evaluate_daily_anomalies", lambda *args: {})
    monkeypatch.setattr(excel, "export_daily_reports", lambda *args, **kwargs: [])
    calls = []

    def collect_business(db, settings, stores, dates):
        calls.append(dict(dates))
        response = {"code": 1, "data": {"code": 200, "data": {
            "currencyCode": "USD", "current": dict(BUSINESS), "chainCompare": dict(BUSINESS),
            "chainCompareStartDate": PRIOR, "chainCompareEndDate": PRIOR}}}
        partition = business_dashboard.persist_business_cards(db, "5174", date.fromisoformat(DAY), response)
        return {"status": "COMPLETE", "partitions": [partition], "metrics": partition["metrics"]}

    monkeypatch.setattr(business_dashboard, "collect_business_for_day", collect_business)
    yield SimpleNamespace(sessions=sessions, source=source, settings=settings, calls=calls,
                          collect_business=collect_business)
    engine.dispose()


def assert_business_saved_without_notifications(warehouse):
    assert warehouse.calls == [{"5174": DAY}]
    with warehouse.sessions() as db:
        facts = list(db.scalars(select(DailyMetric).where(DailyMetric.dimension == DIMENSION)))
        assert len(facts) == 14
        for day in (DAY, PRIOR):
            assert {row.metric_name: row.value for row in facts
                    if row.metric_date.isoformat() == day} == BUSINESS
        assert db.scalar(select(func.count(NotificationOutbox.id))) == 0
        assert db.scalar(select(func.count(NotificationLog.id))) == 0


def test_real_api_unavailable_prerequisites_still_reach_mcp_business_pipeline(warehouse):
    result = operations.run_collection(report_date=DAY, backfill_days=2)
    store = result["stores"]["5174"]
    assert result["status"] == store["status"] == "PARTIAL"
    assert result["failed_store_ids"] == ["5174"]
    assert result["completed_store_dates"] == {}
    assert store["pre_collection_failures"] == [
        {"step": "sp_validation", "report_date": DAY, "error_type": "LingxingError"},
        {"step": "sales_history", "report_date": PRIOR, "error_type": "LingxingError"},
    ]
    assert store["sp"]["status"] == "PARTIAL" and store["sp"]["runs"] == []
    assert store["collection"]["status"] == "PARTIAL_FAILED"
    assert store["collection"]["steps"]["sales"]["status"] == "PARTIAL_FAILED"
    assert store["collection"]["steps"]["business_dashboard"]["metrics"] == 14
    assert_business_saved_without_notifications(warehouse)


@pytest.mark.parametrize("step", ["sp_validation", "sales_history"])
def test_auxiliary_source_failure_keeps_retry_even_if_daily_collection_succeeds(warehouse, monkeypatch, step):
    def failed(*args, **kwargs):
        raise LingxingError("synthetic-private-value-must-not-be-recorded")

    monkeypatch.setattr(operations, "collect_sp_targets", failed if step == "sp_validation"
                        else lambda *args, **kwargs: {"status": "COMPLETE", "runs": []})
    monkeypatch.setattr(sales_collector, "collect_sales_for_day", failed)

    def successful_daily(client, db, dates, **kwargs):
        warehouse.collect_business(db, warehouse.settings, kwargs["stores_response"], dates)
        return {"status": "SUCCEEDED"}

    monkeypatch.setattr(pipeline, "collect_daily_snapshot", successful_daily)
    result = operations.run_collection(report_date=DAY, backfill_days=2 if step == "sales_history" else 1)
    assert result["status"] == "PARTIAL" and result["completed_store_ids"] == []
    assert result["stores"]["5174"]["pre_collection_failures"][0]["step"] == step
    assert "synthetic-private-value" not in json.dumps(result)
    assert_business_saved_without_notifications(warehouse)


@pytest.mark.parametrize("step", ["sp_validation", "sales_history"])
@pytest.mark.parametrize("error", [LingxingError, LingxingCollectionPaused, ValueError])
def test_only_dual_source_provider_errors_continue_daily_pipeline(warehouse, monkeypatch, step, error):
    # Pure API failures retain their previous fail-fast behavior. Pausing and
    # invalid local configuration also stop dual-source work before the pipeline.
    if error is LingxingError:
        primary = SimpleNamespace(get_stores=lambda: {
            "data": [{"sid": 5174, "name": "Geteen US", "status": 1, "has_ads_setting": 1}]})
        monkeypatch.setattr(source_gateway, "create_collection_client", lambda settings: primary)
        monkeypatch.setattr(pipeline, "ad_account_inventory", lambda client: None)

    def failed(*args, **kwargs):
        raise error("private-detail")

    monkeypatch.setattr(operations, "collect_sp_targets", failed if step == "sp_validation"
                        else lambda *args, **kwargs: {"status": "DISABLED", "runs": []})
    monkeypatch.setattr(sales_collector, "collect_sales_for_day", failed)
    monkeypatch.setattr(pipeline, "collect_daily_snapshot", lambda *args, **kwargs:
                        pytest.fail("Failed prerequisites must stop this source/configuration"))
    result = operations.run_collection(report_date=DAY, backfill_days=2)
    assert result["status"] == "PARTIAL"
    assert result["stores"]["5174"] == {"status": "FAILED", "error_type": error.__name__}
    assert warehouse.calls == []
    with warehouse.sessions() as db:
        assert db.scalar(select(func.count(DailyMetric.id))) == 0
        assert db.scalar(select(func.count(NotificationOutbox.id))) == 0
