from datetime import date, datetime
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
        db.commit()

    monkeypatch.setattr(mcp_server, "SessionLocal", Session)
    monkeypatch.setattr(mcp_server, "init_db", lambda: None)

    stores = mcp_server.get_stores()
    assert stores[0]["sid"] == "101"
    assert stores[0]["timezone"] == "America/Los_Angeles"

    metrics = mcp_server.get_daily_metrics("101", "2026-11-10")
    assert metrics[0]["metric_name"] == "orders"
    assert metrics[0]["value"] == 5

    campaigns = mcp_server.get_campaigns(store_id="101", campaign_type="SP")
    assert campaigns[0]["campaign_id"] == "campaign-1"

    coverage = mcp_server.get_data_coverage("101", "2026-11-10", "2026-11-10")
    assert coverage[0]["status"] == "COMPLETE"

    anomalies = mcp_server.get_all_anomalies(store_id="101", severity="P1")
    assert anomalies[0]["rule"] == "orders_decrease"

    jobs = mcp_server.get_sync_jobs()
    assert jobs[0]["status"] == "SUCCEEDED"
