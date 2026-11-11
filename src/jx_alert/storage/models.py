from datetime import datetime, date, timezone
from sqlalchemy import String, Integer, Float, Date, DateTime, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from jx_alert.storage.db import Base

class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("store_id", "metric_date", "dimension", "entity_id", "metric_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    dimension: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(200))
    metric_name: Mapped[str] = mapped_column(String(80))
    value: Mapped[float] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="lingxing")

class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    store_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    payload: Mapped[dict] = mapped_column(JSON)

class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    rule: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(200), default="")
    severity: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20), default="NEW")
    evaluation_scope: Mapped[str] = mapped_column(String(200), default="", index=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    metrics: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)

class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(512), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))



class Store(Base):
    __tablename__ = "stores"

    sid: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    mid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    seller_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    region: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    marketplace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=0)
    has_ads_setting: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "seller_sku", name="uq_products_store_seller_sku"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    seller_sku: Mapped[str] = mapped_column(String(200))
    asin: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    local_sku: Mapped[str | None] = mapped_column(String(200), nullable=True)
    product_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    owner_open_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )



class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "campaign_type", "campaign_id",
            name="uq_campaigns_store_type_campaign",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    campaign_type: Mapped[str] = mapped_column(String(8))
    campaign_id: Mapped[str] = mapped_column(String(100))
    profile_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    serving_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_over_budget_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class AdProduct(Base):
    __tablename__ = "ad_products"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "campaign_type", "ad_id",
            name="uq_ad_products_store_type_ad",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    campaign_type: Mapped[str] = mapped_column(String(8))
    ad_id: Mapped[str] = mapped_column(String(100))
    campaign_id: Mapped[str] = mapped_column(String(100), index=True)
    ad_group_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    asin: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    seller_sku: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    serving_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )



class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    store_count: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)



class DataCoverage(Base):
    __tablename__ = "data_coverage"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "metric_date", "dimension",
            name="uq_data_coverage_store_date_dimension",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    dimension: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="COMPLETE")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
