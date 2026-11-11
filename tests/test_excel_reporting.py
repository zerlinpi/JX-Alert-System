from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import create_engine

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox  # noqa: F401
from jx_alert.storage.models import (
    AdProduct,
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    Product,
    Store,
    SyncJob,
)
from jx_alert.reporting.excel import (
    build_daily_report_rows,
    export_daily_report,
    export_daily_reports,
)
from sqlalchemy.orm import sessionmaker


def test_excel_report_contains_store_sku_campaign_and_alert_rows(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    target = date(2026, 11, 10)
    with Session() as db:
        db.add(Store(
            sid="101", name="Store A", country="United States",
            currency_code="USD", status=1, has_ads_setting=1,
        ))
        db.add(Product(
            store_id="101", seller_sku="SKU-1", asin="B000000001",
            local_sku="LOCAL-1", product_name="Sample",
        ))
        db.add(Campaign(
            store_id="101", campaign_type="sp", campaign_id="campaign-1",
            name="SP Campaign", state="enabled", budget=50, budget_type="daily",
        ))
        db.add(AdProduct(
            store_id="101", campaign_type="sp", ad_id="ad-1",
            campaign_id="campaign-1", asin="B000000001", seller_sku="SKU-1",
        ))
        db.add(DataCoverage(
            store_id="101", metric_date=target, dimension="store",
            status="COMPLETE", row_count=1,
        ))
        for offset in range(1, 8):
            day = target - timedelta(days=offset)
            db.add(DataCoverage(
                store_id="101", metric_date=day, dimension="store",
                status="COMPLETE", row_count=1,
            ))
            for metric_name, metric_value in {
                "orders": 10, "units": 10, "sales": 100,
            }.items():
                db.add(DailyMetric(
                    store_id="101", metric_date=day, dimension="store",
                    entity_id="101", metric_name=metric_name, value=metric_value,
                    currency="USD", source="test",
                ))

        for offset in range(7, -1, -1):
            db.add(DataCoverage(
                store_id="101", metric_date=target - timedelta(days=offset),
                dimension="sku_msku", status="COMPLETE", row_count=1,
            ))
        for offset in range(7, 0, -1):
            db.add(DailyMetric(
                store_id="101", metric_date=target - timedelta(days=offset),
                dimension="sku_msku", entity_id="SKU-1", metric_name="orders",
                value=2, currency="USD", source="test",
            ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="store",
            entity_id="101", metric_name="orders", value=5, currency="USD", source="test",
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="store",
            entity_id="101", metric_name="sales", value=50, currency="USD", source="test",
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="store",
            entity_id="101", metric_name="units", value=15, currency="USD", source="test",
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="sku_msku",
            entity_id="SKU-1", metric_name="orders", value=5, currency="USD", source="test",
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="sku_msku",
            entity_id="SKU-1", metric_name="sales", value=100, currency="USD", source="test",
        ))
        for offset in range(1, 8):
            day = target - timedelta(days=offset)
            db.add(DataCoverage(
                store_id="101", metric_date=day, dimension="campaign_sp",
                status="COMPLETE", row_count=1,
            ))
            for metric_name, metric_value in {"spend": 10, "orders": 1}.items():
                db.add(DailyMetric(
                    store_id="101", metric_date=day, dimension="campaign_sp",
                    entity_id="campaign-1", metric_name=metric_name, value=metric_value,
                    currency="USD", source="test",
                ))
        db.add(DataCoverage(
            store_id="101", metric_date=target, dimension="campaign_sp",
            status="COMPLETE", row_count=1,
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="campaign_sp",
            entity_id="campaign-1", metric_name="spend", value=20,
            currency="USD", source="test",
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="campaign_sp",
            entity_id="campaign-1", metric_name="sales", value=80,
            currency="USD", source="test",
        ))
        db.add(DataCoverage(
            store_id="101", metric_date=target, dimension="product_ad_sp",
            status="COMPLETE", row_count=1,
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="product_ad_sp",
            entity_id="ad-1", metric_name="spend", value=5,
            currency="USD", source="test",
        ))
        db.add(AnomalyEvent(
            dedupe_key="101:SKU-1:orders_decrease", store_id="101",
            entity_id="SKU-1", rule="orders_decrease", severity="P1",
            status="NEW", evaluation_scope="101:sales:sku_msku",
            first_seen=datetime(2026, 11, 11, 8),
            last_seen=datetime(2026, 11, 11, 8),
            metrics={"current": 5}, reason="test anomaly",
        ))
        db.add(SyncJob(
            job_type="daily_collection", status="SUCCEEDED",
            started_at=datetime(2026, 11, 11, 8),
            finished_at=datetime(2026, 11, 11, 8, 1),
            store_count=1, record_count=10,
            metadata_json={},
        ))
        db.commit()

        rows = build_daily_report_rows(db, target)
        assert rows["店铺总览"][0][0:2] == ["101", "Store A"]
        sku_row = rows["SKU销量"][0]
        assert sku_row[3] == "SKU-1"
        assert sku_row[9] == 2
        assert sku_row[10] == 1.5
        assert sku_row[13] == 2
        assert sku_row[14] == 1.5
        store_row = rows["店铺总览"][0]
        assert store_row[10] == 10
        assert store_row[11] == -0.5
        assert store_row[12] == 10
        assert store_row[13] == -0.5
        assert rows["广告活动"][0][3] == "campaign-1"
        campaign_row = rows["广告活动"][0]
        assert campaign_row[16] == 10
        assert campaign_row[17] == 1.0
        assert campaign_row[20] == 10
        assert campaign_row[21] == 1.0
        assert rows["广告商品"][0][3] == "ad-1"
        assert rows["异常记录"][0][4] == "orders_decrease"
        assert rows["采集日志"][0][1] == "daily_collection"

        path = export_daily_report(tmp_path / "daily.xlsx", rows)
        workbook = load_workbook(path, read_only=True)
        assert workbook.sheetnames == [
            "店铺总览", "店铺绩效", "SKU销量", "广告活动", "广告商品", "异常记录", "采集日志"
        ]
        assert workbook["SKU销量"].max_row == 2
        workbook.close()

        store_paths = export_daily_reports(
            db, target, tmp_path / "reports", store_id="101"
        )
        assert len(store_paths) == 1
        assert Path(store_paths[0]).exists()
