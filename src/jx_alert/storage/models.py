from datetime import datetime, date
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
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    payload: Mapped[dict] = mapped_column(JSON)

class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    store_id: Mapped[str] = mapped_column(String(100), index=True)
    rule: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20), default="NEW")
    evaluation_scope: Mapped[str] = mapped_column(String(200), default="", index=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
