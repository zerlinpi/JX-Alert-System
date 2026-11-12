from datetime import date
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import AdProduct, DataCoverage, DailyMetric, Product, RawSnapshot
from jx_alert.lingxing.product_ads_collector import collect_product_ads_for_day


class FakeProductAdsClient:
    def get_stores(self):
        return {"code": 0, "data": [{
            "sid": 101, "name": "Store A", "status": 1, "has_ads_setting": 1,
        }]}

    def iter_advertising_product_ads(self, report_type, payload, **kwargs):
        return iter([{
            "ad_id": 5001 if report_type == "sp" else 6001,
            "campaign_id": 7001 if report_type == "sp" else 8001,
            "ad_group_id": 9001,
            "sku": "SKU-1",
            "asin": "B000000001",
            "state": "enabled",
            "serving_status": "AD_STATUS_LIVE",
        }])

    def iter_advertising_product_report(self, report_type, payload, **kwargs):
        return iter([{
            "ad_id": 5001 if report_type == "sp" else 6001,
            "campaign_id": 7001 if report_type == "sp" else 8001,
            "ad_group_id": 9001,
            "sku": "SKU-1",
            "asin": "B000000001",
            "report_date": payload["report_date"],
            "cost": 12.5,
            "orders": 2,
            "orders_7d": 3,
            "same_orders": 1,
            "sales": 50,
            "sales_7d": 55,
            "units": 2,
            "clicks": 10,
            "impressions": 1000,
        }])


def test_product_ads_collector_saves_sp_sd_metrics_and_mappings():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_product_ads_for_day(
            FakeProductAdsClient(), db, "2026-11-10"
        )
        assert result == {
            "stores_seen": 1,
            "active_ad_stores": 1,
            "master_ads": {"sp": 1, "sd": 1},
            "rows": {"sp": 1, "sd": 1},
        }
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 18
        assert db.scalar(select(func.count()).select_from(AdProduct)) == 2
        assert db.scalar(select(func.count()).select_from(Product)) == 1
        sp_spend = db.scalar(select(DailyMetric).where(
            DailyMetric.dimension == "product_ad_sp",
            DailyMetric.metric_name == "spend",
        ))
        assert sp_spend.value == 12.5
        assert sp_spend.metric_date == date(2026, 11, 10)
        sp_orders_7d = db.scalar(select(DailyMetric).where(
            DailyMetric.dimension == "product_ad_sp",
            DailyMetric.metric_name == "orders_7d",
        ))
        assert sp_orders_7d.value == 3
        product = db.scalar(select(Product))
        assert product.seller_sku == "SKU-1"
        assert product.asin == "B000000001"
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 4
        assert db.scalar(select(func.count()).select_from(DataCoverage)) == 2


def test_multiple_ads_share_one_product_with_production_autoflush_disabled():
    class Client(FakeProductAdsClient):
        def iter_advertising_product_ads(self, report_type, payload, **kwargs):
            row = list(super().iter_advertising_product_ads(report_type, payload))[0]
            return iter([row, {**row, "ad_id": row["ad_id"] + 100}])

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        collect_product_ads_for_day(Client(), db, "2026-11-10")
        assert db.scalar(select(func.count()).select_from(Product)) == 1
        assert db.scalar(select(func.count()).select_from(AdProduct)) == 4


def test_duplicate_product_report_rows_preserve_prior_partition():
    import pytest

    class Duplicate(FakeProductAdsClient):
        def iter_advertising_product_report(self, report_type, payload, **kwargs):
            rows = list(super().iter_advertising_product_report(report_type, payload))
            return iter(rows + rows)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        collect_product_ads_for_day(FakeProductAdsClient(), db, "2026-11-10")
        before = db.scalar(select(func.count()).select_from(DailyMetric))
        with pytest.raises(RuntimeError, match="duplicate ad_id"):
            collect_product_ads_for_day(Duplicate(), db, "2026-11-10")
        db.rollback()
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == before


def test_product_ads_collector_replaces_same_day_metrics_idempotently():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        client = FakeProductAdsClient()
        collect_product_ads_for_day(client, db, "2026-11-10")
        collect_product_ads_for_day(client, db, "2026-11-10")
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 18



def test_product_ads_collector_refreshes_multiple_report_dates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_product_ads_for_day(
            FakeProductAdsClient(),
            db,
            {"101": ["2026-11-09", "2026-11-10"]},
        )
        assert result["rows"] == {"sp": 2, "sd": 2}
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 36
        assert db.scalar(select(func.count()).select_from(DataCoverage)) == 4



def test_product_ad_master_sync_tracks_paused_and_missing_ads():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    class ChangedStateClient(FakeProductAdsClient):
        def iter_advertising_product_ads(self, report_type, payload, **kwargs):
            if report_type == "sd":
                return iter([])
            return iter([{
                "ad_id": 5001,
                "campaign_id": 7001,
                "ad_group_id": 9001,
                "sku": "SKU-1",
                "asin": "B000000001",
                "state": "paused",
                "serving_status": "CAMPAIGN_PAUSED",
            }])

    with Session() as db:
        client = FakeProductAdsClient()
        collect_product_ads_for_day(client, db, "2026-11-10")
        collect_product_ads_for_day(ChangedStateClient(), db, "2026-11-10")
        sp = db.scalar(select(AdProduct).where(AdProduct.campaign_type == "sp"))
        sd = db.scalar(select(AdProduct).where(AdProduct.campaign_type == "sd"))
        assert sp.state == "paused"
        assert sp.serving_status == "CAMPAIGN_PAUSED"
        assert sd.state == "not_returned"
        assert sd.serving_status == "not_returned"
