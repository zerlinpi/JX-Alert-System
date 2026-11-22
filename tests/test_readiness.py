import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.readiness import analysis_plan, configuration_status
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store, SyncJob


def test_manual_roles_are_reported_without_identifiers_or_secrets(tmp_path):
    routing, scope = tmp_path / "routing.json", tmp_path / "scope.json"
    routing.write_text(json.dumps({"default_chat_id": "test", "groups": {
        "test": {"chat_id": "oc_private_group", "admin_open_ids": ["ou_private_admin"]}},
        "stores": {"5174": {"open_ids": []}}, "management": {"open_ids": []}}))
    scope.write_text(json.dumps({"store_ids": [5174]}))
    settings = SimpleNamespace(feishu_routing_file=str(routing), collection_scope_file=str(scope),
                               lingxing_performance_source_timezone="", mcp_public_url="",
                               scheduler_enabled=True, daily_sync_cron="30 18 * * *", app_timezone="Asia/Shanghai",
                               mcp_auth_mode="bearer", lingxing_performance_enabled=True)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid="5174", name="selected", timezone="America/Los_Angeles", currency_code="USD"))
        db.flush()
        report = configuration_status(db, settings)
        assert report['stores'][0]['admin_count'] == 1
        assert report['stores'][0]['timezone_valid']
        assert {item['field'] for item in report['manual_mapping_required']} == {
            'operator_count', 'management.open_ids', 'verified_health_source_timezone', 'mcp_public_url_dns_tls'}
        assert 'ou_private_admin' not in json.dumps(report) and 'oc_private_group' not in json.dumps(report)
        routing.write_text(json.dumps({"groups": [], "stores": None, "management": "bad"}))
        broken = configuration_status(db, settings)
        assert broken['stores'][0]['admin_count'] == 0 and broken['manager_count'] == 0


def test_collection_status_tracks_the_active_schedule_and_separates_report_coverage(tmp_path):
    routing, scope = tmp_path / "routing.json", tmp_path / "scope.json"
    routing.write_text("{}")
    scope.write_text("{}")
    settings = SimpleNamespace(feishu_routing_file=str(routing), collection_scope_file=str(scope),
        lingxing_performance_source_timezone="", mcp_public_url="", scheduler_enabled=True,
        daily_sync_cron="0 8 * * *", hourly_ad_monitor_enabled=True, hourly_ad_monitor_cron="5 * * * *",
        mcp_auth_mode="bearer", lingxing_performance_enabled=True)
    def timestamp(hour, minute=0):
        return datetime(2026, 11, 19, hour, minute)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        hourly = SyncJob(job_type="hourly_ad_monitor", status="SUCCEEDED",
            started_at=timestamp(6), finished_at=timestamp(6, 4), metadata_json={"stores": {
                "5174": {"status": "COMPLETE", "reports": {"2026-11-17": {"status": "AVAILABLE"},
                                                          "2026-11-16": {"status": "EMPTY"}}},
                "5175": {"status": "COMPLETE", "reports": {"2026-11-17": {"status": "EMPTY"}}},
            }})
        daily = SyncJob(job_type="daily_collection", status="SUCCEEDED", store_count=3,
                        started_at=timestamp(8), finished_at=timestamp(8, 10))
        failed = SyncJob(job_type="hourly_ad_monitor", status="PARTIAL_FAILED", started_at=timestamp(7),
            finished_at=timestamp(7, 9), metadata_json={"stores": {
                "5174": {"status": "PARTIAL", "reports": {"2026-11-17": {"status": "FAILED"}}},
            }})
        backfilled = SyncJob(job_type="hourly_ad_monitor", status="SUCCEEDED", started_at=timestamp(4),
                             finished_at=timestamp(4, 6))
        scheduled = SyncJob(job_type="manual_collection", status="PENDING", started_at=timestamp(10, 30),
            metadata_json={"idempotency_key": "scheduled-collection:2026-11-19"})
        user_backfill = SyncJob(job_type="manual_collection", status="SUCCEEDED", started_at=timestamp(11),
            metadata_json={"idempotency_key": "ordinary-backfill"})
        db.add_all([hourly, daily, failed, backfilled, scheduled, user_backfill])
        db.flush()
        current = configuration_status(db, settings)
        assert current["collection_mode"] == "hourly_ad_monitor"
        assert current["cron"] == "5 * * * *"
        assert current["scheduled_collection_cron"] == "30 18 * * *"
        assert current["scheduled_collection_timezone"] == "Asia/Shanghai"
        assert current["last_scheduled_collection_attempt"]["job_id"] == scheduled.id
        assert current["last_scheduled_collection_attempt"]["status"] == "PENDING"
        assert current["last_successful_collection"] == {
            "job_id": hourly.id, "job_type": "hourly_ad_monitor", "status": "SUCCEEDED",
            "started_at_utc": "2026-11-19 06:00:00", "finished_at_utc": "2026-11-19 06:04:00",
            "store_count": 2, "sp_report_partition_counts": {"AVAILABLE": 1, "EMPTY": 2},
        }
        assert current["last_collection_attempt"]["job_id"] == failed.id
        assert current["last_collection_attempt"]["sp_report_partition_counts"] == {"FAILED": 1}

        settings.hourly_ad_monitor_enabled = False
        current = configuration_status(db, settings)
        assert current["collection_mode"] == "daily_collection"
        assert current["cron"] == "0 8 * * *"
        assert current["scheduled_collection_cron"] == "0 8 * * *"
        assert current["last_successful_collection"]["job_id"] == daily.id
        assert current["last_collection_attempt"]["store_count"] == 3
        assert "sp_report_partition_counts" not in current["last_collection_attempt"]
    engine.dispose()


def test_unsynced_stores_and_unknown_hourly_timezones_are_not_collection_ready(tmp_path):
    scope, zones = tmp_path / "scope.json", tmp_path / "zones.json"
    scope.write_text(json.dumps({"store_ids": [5174, 5175, 5176]}))
    zones.write_text(json.dumps({"stores": {
        sid: {"timezone": "America/Los_Angeles"} for sid in ("5174", "5175")
    }}))
    settings = SimpleNamespace(collection_scope_file=str(scope), store_timezones_file=str(zones),
        store_controls_file=str(tmp_path / "controls.json"), hourly_ad_monitor_enabled=True,
        allow_unconfigured_timezone_history=True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Store(sid=sid, name=sid, status=1) for sid in ("5174", "5176")])
        db.flush()
        plan = analysis_plan(db, settings, now=datetime(2026, 11, 19, 6, tzinfo=timezone.utc))
        rows = {row["store_id"]: row for row in plan["stores"]}
        assert plan["collection_ready_count"] == 1
        assert rows["5174"]["collection_ready"]
        assert rows["5175"]["state"] == "WAITING_STORE_SYNC"
        assert rows["5175"]["status_code"] is None
        assert not rows["5175"]["intraday_enabled"]
        assert not rows["5175"]["collection_ready"]
        assert rows["5176"]["date_basis"] == "global_complete_unknown_timezone"
        assert not rows["5176"]["collection_ready"]
    engine.dispose()


@pytest.mark.parametrize("attempts,expected", [
    (("ad_history_only",), "AVAILABLE"),
    (("sales_succeeded_before_ads_failed",), "AVAILABLE"),
    (("sales_failed",), "COLLECTION_FAILED"),
    (("sales_failed", "ad_history_only"), "COLLECTION_FAILED"),
])
def test_sales_readiness_resolves_the_exact_sales_attempt(tmp_path, attempts, expected):
    from jx_alert.mcp_server import _daily_summary

    sid = "101"
    target = date(2026, 11, 20)
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"store_ids": [sid]}))
    zones = tmp_path / "zones.json"
    zones.write_text(json.dumps({"stores": {sid: {"timezone": "America/Los_Angeles"}}}))
    settings = SimpleNamespace(collection_scope_file=str(scope), store_timezones_file=str(zones),
                               store_controls_file=str(tmp_path / "controls.json"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid=sid, name="Store", status=1, has_ads_setting=1,
                     timezone="America/Los_Angeles", currency_code="USD"))
        for on, orders in ((target - timedelta(days=1), 5), (target, 7)):
            for dimension in ("store", "sku_asin", "sku_msku", "campaign_sp"):
                db.add(DataCoverage(store_id=sid, metric_date=on, dimension=dimension,
                                    status="COMPLETE", row_count=1))
            for metric, value in (("orders", orders), ("units", orders + 1), ("sales", orders * 10)):
                db.add(DailyMetric(store_id=sid, entity_id=sid, metric_date=on,
                                   dimension="store", metric_name=metric, value=value, currency="USD"))
        db.add(DailyMetric(store_id=sid, entity_id="sp1", metric_date=target, dimension="campaign_sp",
                           metric_name="spend", value=12.5, currency="USD"))
        for attempt in attempts:
            report_day = target + timedelta(days=int(attempt == "ad_history_only"))
            completed = ["store_status"]
            if attempt != "sales_failed":
                completed.append("sales")
            db.add(SyncJob(job_type="daily_collection", status="PARTIAL_FAILED", metadata_json={
                "report_dates": {sid: report_day.isoformat()},
                "ad_report_dates": {sid: [target.isoformat(), report_day.isoformat()]},
                "completed_steps": completed,
            }))
        db.commit()
        now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
        plan = analysis_plan(db, settings, now=now)["stores"][0]
        actual = _daily_summary(db, sid, target)
        ready = expected == "AVAILABLE"
        assert plan["report_date"] == target.isoformat()
        assert plan["sales_data_status"] == actual["sales_data_status"] == expected
        assert plan["analysis_ready"] is ready
        assert actual["sales"]["orders"]["current"] == (7 if ready else None)
        assert actual["sales"]["orders"]["previous_day"] == 5
        assert actual["coverage"]["campaign_sp"] is True
        assert actual["campaign_ads"]["by_type"]["sp"]["totals"]["spend"] == 12.5
    engine.dispose()
