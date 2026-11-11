from datetime import date, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.notifications.outbox import NotificationOutbox  # noqa: F401
from jx_alert.storage.models import (
    AnomalyEvent,
    NotificationLog,
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
        db.add(NotificationOutbox(
            idempotency_key="mcp-test-alert",
            chat_id="default",
            payload={
                "text": "test notification",
                "store_id": "101",
                "entity_id": "SKU-1",
                "open_ids": [],
            },
            status="DELIVERED",
            attempts=1,
            next_attempt_at=datetime(2026, 11, 11, 8),
            delivered_at=datetime(2026, 11, 11, 8),
        ))
        db.add(NotificationLog(
            dedupe_key="mcp-test-alert",
            channel="feishu_webhook",
            status="DELIVERED",
            message_id=None,
            error=None,
            created_at=datetime(2026, 11, 11, 8),
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
        for dimension in ("sku_msku", "sku_asin", "campaign_sp"):
            for offset in range(1, 8):
                day = target - timedelta(days=offset)
                db.add(DataCoverage(
                    store_id="101", metric_date=day, dimension=dimension,
                    status="COMPLETE", row_count=1,
                ))
                if dimension == "sku_msku":
                    entity_id = "SKU-1"
                    metrics_for_day = {"orders": 10, "units": 12, "sales": 100}
                elif dimension == "sku_asin":
                    entity_id = "B000000001"
                    metrics_for_day = {"orders": 8, "units": 9, "sales": 80}
                else:
                    entity_id = "campaign-1"
                    metrics_for_day = {"spend": 10, "orders": 1, "sales": 40, "clicks": 8, "impressions": 800}
                for metric_name, metric_value in metrics_for_day.items():
                    db.add(DailyMetric(
                        store_id="101", metric_date=day, dimension=dimension,
                        entity_id=entity_id, metric_name=metric_name, value=metric_value,
                        currency="USD", source="test",
                    ))
        for metric_name, metric_value in {
            "orders": 5, "units": 6, "sales": 50,
        }.items():
            db.add(DailyMetric(
                store_id="101", metric_date=target, dimension="sku_msku",
                entity_id="SKU-1", metric_name=metric_name, value=metric_value,
                currency="USD", source="test",
            ))
        for metric_name, metric_value in {
            "orders": 12, "units": 13, "sales": 120,
        }.items():
            db.add(DailyMetric(
                store_id="101", metric_date=target, dimension="sku_asin",
                entity_id="B000000001", metric_name=metric_name, value=metric_value,
                currency="USD", source="test",
            ))

        db.commit()

    monkeypatch.setattr(mcp_server, "SessionLocal", Session)
    monkeypatch.setattr(mcp_server, "init_db", lambda: None)

    stores = mcp_server.get_stores()
    assert stores[0]["sid"] == "101"
    assert stores[0]["timezone"] == "America/Los_Angeles"

    metrics = mcp_server.get_daily_metrics("101", "2026-11-10")
    orders_metric = next(
        row for row in metrics
        if row["dimension"] == "store" and row["metric_name"] == "orders"
    )
    assert orders_metric["value"] == 5

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

    sku_rows = mcp_server.get_sku_performance("101", "2026-11-10")
    sku_row = next(row for row in sku_rows if row["entity_id"] == "SKU-1")
    assert sku_row["metrics"]["orders"]["current"] == 5
    assert sku_row["metrics"]["orders"]["average_7d"] == 10
    assert sku_row["metrics"]["orders"]["change_vs_7d_average"] == -0.5

    campaign_rows = mcp_server.get_campaign_performance("101", "2026-11-10", "sp")
    assert campaign_rows[0]["metrics"]["spend"]["current"] == 12
    assert campaign_rows[0]["metrics"]["spend"]["average_7d"] == 10

    history = mcp_server.get_anomaly_history(store_id="101", entity_id="101")
    assert history[0]["rule"] == "orders_decrease"

    notifications = mcp_server.get_notification_logs(store_id="101")
    assert notifications[0]["attempt_status"] == "DELIVERED"
    assert notifications[0]["outbox_status"] == "DELIVERED"
    assert notifications[0]["attempts"] == 1

    jobs = mcp_server.get_sync_jobs()
    assert jobs[0]["status"] == "SUCCEEDED"
