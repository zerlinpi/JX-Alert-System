from datetime import date, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox  # noqa: F401
from jx_alert.storage.models import (
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    Product,
    Store,
    SyncJob,
)
import jx_alert.mcp_server as mcp_server


def test_mcp_tools_query_only_persisted_records(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Store(
            sid="101", name="Store A", status=1, has_ads_setting=1,
            timezone="America/Los_Angeles", currency_code="USD",
        ))
        db.add(DailyMetric(
            store_id="101", metric_date=date(2026, 11, 10),
            dimension="store", entity_id="101", metric_name="orders",
            value=5, currency="USD", source="test",
        ))
        db.add(DataCoverage(
            store_id="101", metric_date=date(2026, 11, 10),
            dimension="store", status="COMPLETE", row_count=1,
        ))
        db.add(Campaign(
            store_id="101", campaign_type="sp", campaign_id="campaign-1",
            name="SP Campaign", state="enabled", budget=50, budget_type="daily",
        ))
        db.add(Product(
            store_id="101", seller_sku="SKU-1", asin="B000000001",
            local_sku="LOCAL-1", product_name="Test product",
            currency_code="USD", owner_open_ids=["ou_product_owner"],
        ))
        db.add(AnomalyEvent(
            dedupe_key="101:101:orders_decrease", store_id="101",
            entity_id="101", rule="orders_decrease", severity="P1",
            status="NEW", evaluation_scope="101:sales:store",
            first_seen=datetime(2026, 11, 11, 8),
            last_seen=datetime(2026, 11, 11, 8),
            metrics={"current": 5}, reason="test alert",
        ))
        db.add(SyncJob(
            job_type="daily_collection", status="SUCCEEDED",
            started_at=datetime(2026, 11, 11, 8),
            finished_at=datetime(2026, 11, 11, 8, 1),
            store_count=1, record_count=5, metadata_json={"ok": True},
        ))
        target = date(2026, 11, 10)
        for offset in range(1, 8):
            day = target - timedelta(days=offset)
            db.add(DataCoverage(
                store_id="101", metric_date=day, dimension="store",
                status="COMPLETE", row_count=1,
            ))
            db.add(DailyMetric(
                store_id="101", metric_date=day, dimension="store",
                entity_id="101", metric_name="orders", value=10,
                currency="USD", source="test",
            ))
        for dimension in (
            "sku_asin", "sku_msku", "campaign_sp", "campaign_sb",
            "campaign_sd", "product_ad_sp", "product_ad_sd",
        ):
            db.add(DataCoverage(
                store_id="101", metric_date=target, dimension=dimension,
                status="COMPLETE", row_count=1,
            ))
        for metric_name, metric_value in {
            "spend": 12, "orders": 2, "sales": 60, "clicks": 10, "impressions": 1000,
        }.items():
            db.add(DailyMetric(
                store_id="101", metric_date=target, dimension="campaign_sp",
                entity_id="campaign-1", metric_name=metric_name, value=metric_value,
                currency="USD", source="test",
            ))
        db.add(DailyMetric(
            store_id="101", metric_date=target, dimension="product_ad_sp",
            entity_id="ad-1", metric_name="spend", value=8,
            currency="USD", source="test",
        ))
        db.commit()

    monkeypatch.setattr(mcp_server, "SessionLocal", Session)
    monkeypatch.setattr(mcp_server, "init_db", lambda: None)

    stores = mcp_server.get_stores()
    assert stores[0]["sid"] == "101"
    assert stores[0]["timezone"] == "America/Los_Angeles"

    metrics = mcp_server.get_daily_metrics("101", "2026-11-10")
    assert metrics[0]["metric_name"] == "orders"
    assert metrics[0]["value"] == 5

    summary = mcp_server.get_daily_summary("101", "2026-11-10")
    assert summary["data_complete"] is True
    assert summary["sales"]["orders"]["current"] == 5
    assert summary["sales"]["orders"]["previous_day"] == 10
    assert summary["sales"]["orders"]["average_7d"] == 10
    assert summary["sales"]["orders"]["change_vs_7d_average"] == -0.5
    assert summary["campaign_ads"]["total"]["spend"] == 12
    assert summary["product_ads"]["by_type"]["sp"]["totals"]["spend"] == 8

    campaigns = mcp_server.get_campaigns(store_id="101", campaign_type="SP")
    assert campaigns[0]["campaign_id"] == "campaign-1"

    products = mcp_server.get_products(store_id="101", seller_sku="SKU-1")
    assert products[0]["asin"] == "B000000001"
    assert products[0]["owner_open_ids"] == ["ou_product_owner"]

    coverage = mcp_server.get_data_coverage("101", "2026-11-10", "2026-11-10")
    assert coverage[0]["status"] == "COMPLETE"

    anomalies = mcp_server.get_all_anomalies(store_id="101", severity="P1")
    assert anomalies[0]["rule"] == "orders_decrease"

    jobs = mcp_server.get_sync_jobs()
    assert jobs[0]["status"] == "SUCCEEDED"
