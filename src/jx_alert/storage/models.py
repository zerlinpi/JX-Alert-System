"""Tables used by the SparkX-only release.

Other tables in an existing database (left by earlier releases) remain as
historical evidence. This release does not map, read, write or drop them.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jx_alert.storage.base import Base


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(512), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SyncJob(Base):
    """Run receipts: report lanes, SparkX roster snapshots and bot questions."""
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    store_count: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
