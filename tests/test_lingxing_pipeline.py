import pytest
from types import SimpleNamespace
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import DataCoverage, DailyMetric, SyncJob
from jx_alert.lingxing.pipeline import collect_daily_snapshot


class FakeDailyClient:
    def __init__(self, fail_product_sd=False):
        self.fail_product_sd = fail_product_sd

    def get_stores(self):
        return {"code": 0, "data": [{
            "sid": 101, "name": "Store A", "status": 1, "has_ads_setting": 1,
        }]}

    def seller_ad_accounts(self):
        return [{"sid": "101", "profile_id": "888", "type": "seller", "currency_code": "CAD"}]

    def sales_daily(self, payload):
        return {"code": 0, "total": 1, "data": [{
            "sid": 101,
            "r_date": payload["event_date"],
            "currency_code": "USD",
            "seller_sku": "SKU-1",
            "asin": "B000000001",
            "product_name": "Sample Product",
            "map_value": {1: 100.0, 2: 5, 3: 3}[payload["type"]],
        }]}

    def iter_advertising_campaigns(self, campaign_type, payload, **kwargs):
        return iter([{
            "campaign_id": {"sp": 1001, "sb": 2001, "sd": 3001}[campaign_type],
            "profile_id": "888",
            "name": f"{campaign_type.upper()} Campaign",
            "state": "enabled",
            "budget": 50,
            "budget_type": "daily",
        }])

    def iter_advertising_report(self, report_type, payload, **kwargs):
        return iter([{
            "campaign_id": {"sp": 1001, "sb": 2001, "sd": 3001}[report_type],
            "report_date": payload["report_date"],
            "cost": 10,
            "orders": 1,
            "sales": 25,
            "units": 1,
            "clicks": 5,
            "impressions": 500,
        }])

    def iter_advertising_product_ads(self, report_type, payload, **kwargs):
        return iter([{
            "ad_id": 5001 if report_type == "sp" else 6001,
            "campaign_id": 1001 if report_type == "sp" else 3001,
            "ad_group_id": 9001,
            "sku": "SKU-1",
            "asin": "B000000001",
            "state": "enabled",
            "serving_status": "AD_STATUS_LIVE",
        }])

    def iter_advertising_product_report(self, report_type, payload, **kwargs):
        if self.fail_product_sd and report_type == "sd":
            raise RuntimeError("simulated SD product report failure")
        return iter([{
            "ad_id": 5001 if report_type == "sp" else 6001,
            "campaign_id": 1001 if report_type == "sp" else 3001,
            "ad_group_id": 9001,
            "sku": "SKU-1",
            "asin": "B000000001",
            "report_date": payload["report_date"],
            "cost": 2,
            "orders": 1,
            "sales": 20,
            "units": 1,
            "clicks": 2,
            "impressions": 100,
        }])


def test_daily_pipeline_records_successful_job_and_all_steps(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_daily_snapshot(
            FakeDailyClient(), db, "2026-11-10",
            ad_attribution_refresh_days=1,
            settings=SimpleNamespace(lingxing_performance_enabled=False, raw_dir=str(tmp_path / "raw")),
        )
        assert result["status"] == "SUCCEEDED"
        assert result["store_count"] == 1
        assert result["record_count"] == 14
        job = db.get(SyncJob, result["job_id"])
        assert job.status == "SUCCEEDED"
        assert job.metadata_json["completed_steps"] == [
            "store_status", "sales", "campaigns", "campaign_reports", "product_ad_reports", "raw_archive"
        ]
        assert db.scalar(select(func.count()).select_from(DailyMetric)) > 0
        assert db.scalar(select(func.count()).select_from(DataCoverage)) == 8
        ad_facts = db.scalars(select(DailyMetric).where(DailyMetric.dimension.in_(
            ["campaign_sp", "campaign_sb", "campaign_sd", "product_ad_sp", "product_ad_sd"]))).all()
        assert all(r.currency == "CAD" if r.metric_name in {"sales", "spend"} else r.currency is None
                   for r in ad_facts)


def test_daily_pipeline_marks_partial_failure_and_keeps_completed_steps(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        with pytest.raises(RuntimeError, match="simulated SD product report failure"):
            collect_daily_snapshot(
                FakeDailyClient(fail_product_sd=True), db, "2026-11-10",
                ad_attribution_refresh_days=1,
                settings=SimpleNamespace(lingxing_performance_enabled=False, raw_dir=str(tmp_path / "raw")),
            )
        job = db.scalar(select(SyncJob))
        assert job.status == "PARTIAL_FAILED"
        assert job.metadata_json["completed_steps"] == [
            "store_status", "sales", "campaigns", "campaign_reports"
        ]
        assert db.scalar(select(func.count()).select_from(DailyMetric)) > 0



def test_daily_pipeline_refreshes_configured_ad_attribution_window(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_daily_snapshot(
            FakeDailyClient(),
            db,
            {"101": "2026-11-10"},
            ad_attribution_refresh_days=2,
            settings=SimpleNamespace(lingxing_performance_enabled=False, raw_dir=str(tmp_path / "raw")),
        )
        assert result["status"] == "SUCCEEDED"
        assert result["steps"]["ad_report_dates"] == {
            "101": ["2026-11-09", "2026-11-10"]
        }
        assert result["steps"]["campaign_reports"]["rows"] == {
            "sp": 2, "sb": 2, "sd": 2,
        }
        assert result["steps"]["product_ad_reports"]["rows"] == {
            "sp": 2, "sd": 2,
        }
