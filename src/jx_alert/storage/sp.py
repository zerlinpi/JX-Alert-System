"""Additive SP report versions. Existing business tables are never altered here."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jx_alert.storage.db import Base


class CollectionLog(Base):
    __tablename__ = "collection_logs"
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    campaign_id: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(40), default="lingxing_sp")
    status: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    row_counts: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)


class _ReportColumns:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collection_logs.run_id"), index=True,
    )
    store_id: Mapped[str] = mapped_column(String(100))
    profile_id: Mapped[str] = mapped_column(String(40))
    campaign_id: Mapped[str] = mapped_column(String(40))
    report_date: Mapped[date] = mapped_column(Date)
    row_key: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime)
    source_snapshot_id: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(32, 12, asdecimal=True))
    clicks: Mapped[int] = mapped_column(BigInteger)
    impressions: Mapped[int] = mapped_column(BigInteger)
    orders: Mapped[int] = mapped_column(BigInteger)
    sales: Mapped[Decimal] = mapped_column(Numeric(32, 12, asdecimal=True))
    raw_json: Mapped[dict] = mapped_column(JSON)


class SPDailyReport(_ReportColumns, Base):
    __tablename__ = "sp_daily_reports"
    __table_args__ = (
        UniqueConstraint("run_id", "row_key", name="uq_sp_daily_version_row"),
        Index("ix_sp_daily_partition", "store_id", "report_date", "campaign_id", "run_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    campaign_name: Mapped[str | None] = mapped_column(String(500), nullable=True)


class SPHourlyReport(_ReportColumns, Base):
    __tablename__ = "sp_hourly_reports"
    __table_args__ = (
        UniqueConstraint("run_id", "row_key", name="uq_sp_hourly_version_row"),
        Index("ix_sp_hourly_partition", "store_id", "report_date", "campaign_id", "run_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    scope: Mapped[str] = mapped_column(String(16))
    hour: Mapped[int] = mapped_column(Integer)
    group_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class SPAdvertiseDetail(_ReportColumns, Base):
    __tablename__ = "sp_advertise_details"
    __table_args__ = (
        UniqueConstraint("run_id", "row_key", name="uq_sp_ad_version_row"),
        Index("ix_sp_ad_partition", "store_id", "report_date", "campaign_id", "run_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    hour: Mapped[int] = mapped_column(Integer)
    ad_id: Mapped[str] = mapped_column(String(40))
    group_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    asin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seller_sku: Mapped[str | None] = mapped_column(String(200), nullable=True)


class SPTargetDetail(_ReportColumns, Base):
    __tablename__ = "sp_target_details"
    __table_args__ = (
        UniqueConstraint("run_id", "row_key", name="uq_sp_target_version_row"),
        Index("ix_sp_target_partition", "store_id", "report_date", "campaign_id", "run_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    hour: Mapped[int] = mapped_column(Integer)
    targeting_id: Mapped[str] = mapped_column(String(40))
    group_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ad_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    asin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seller_sku: Mapped[str | None] = mapped_column(String(200), nullable=True)
    match_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    targeting: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReconciliationLog(Base):
    __tablename__ = "reconciliation_logs"
    __table_args__ = (
        UniqueConstraint("run_id", "report_date", "scope", name="uq_sp_reconciliation_version"),
        Index("ix_sp_reconciliation_lookup", "store_id", "campaign_id", "report_date", "scope"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collection_logs.run_id"), index=True,
    )
    store_id: Mapped[str] = mapped_column(String(100))
    campaign_id: Mapped[str] = mapped_column(String(40))
    report_date: Mapped[date] = mapped_column(Date)
    scope: Mapped[str] = mapped_column(String(16))
    captured_at: Mapped[datetime] = mapped_column(DateTime)
    collection_status: Mapped[str] = mapped_column(String(24))
    comparison_status: Mapped[str] = mapped_column(String(24))
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hourly_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    delta_hour_minus_daily: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
