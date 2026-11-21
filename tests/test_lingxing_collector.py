from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import DataCoverage, DailyMetric, RawSnapshot
from jx_alert.lingxing.collector import SPPartitionBusy, collect_ads_for_day


class FakeLingxingClient:
    def get_stores(self):
        return {
            "code": 0,
            "data": [
                {"sid": 101, "name": "Store A", "status": 1, "has_ads_setting": 1},
                {"sid": 102, "name": "Store B", "status": 1, "has_ads_setting": 0},
            ],
        }

    def iter_advertising_report(self, report_type, payload):
        assert payload["sid"] == 101
        row = {
            "campaign_id": 9001,
            "report_date": payload["report_date"],
            "cost": 12.5,
            "orders": 2,
            "sales": 50,
            "units": 2,
            "clicks": 10,
            "impressions": 1000,
            "same_orders": 1,
        }
        if report_type in {"sp", "sd"}:
            assert payload["show_detail"] == 1
            row["orders_7d"] = 3
        return iter([row])


def test_collect_ads_persists_raw_and_normalized_rows():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_ads_for_day(FakeLingxingClient(), db, "2026-11-10")
        assert result == {
            "report_date": "2026-11-10",
            "stores_seen": 2,
            "active_ad_stores": 1,
            "report_days_per_store": 1,
            "rows": {"sp": 1, "sb": 1, "sd": 1},
        }
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 23
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 4
        assert db.scalar(select(func.count()).select_from(DataCoverage)) == 3
        spend = db.scalar(select(DailyMetric).where(DailyMetric.metric_name == "spend"))
        assert spend.value == 12.5
        assert spend.metric_date == date(2026, 11, 10)
        orders_7d = db.scalar(select(DailyMetric).where(
            DailyMetric.dimension == "campaign_sp",
            DailyMetric.metric_name == "orders_7d",
        ))
        assert orders_7d.value == 3


def test_collect_ads_replaces_same_day_metrics_idempotently():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        client = FakeLingxingClient()
        collect_ads_for_day(client, db, "2026-11-10")
        collect_ads_for_day(client, db, "2026-11-10")
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 23



def test_collect_ads_accepts_store_local_date_map():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_ads_for_day(
            FakeLingxingClient(), db, {"101": "2026-11-09"}
        )
        assert result["report_date"] == "per-store date window"
        metric = db.scalar(select(DailyMetric))
        assert metric.metric_date == date(2026, 11, 9)



def test_collect_ads_refreshes_multiple_store_local_dates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_ads_for_day(
            FakeLingxingClient(),
            db,
            {"101": ["2026-11-09", "2026-11-10"]},
        )
        assert result["report_days_per_store"] == "per-store date window"
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 46
        assert db.scalar(select(func.count()).select_from(DataCoverage)) == 6
        dates = db.scalars(select(DataCoverage.metric_date).where(
            DataCoverage.dimension == "campaign_sp"
        ).order_by(DataCoverage.metric_date)).all()
        assert dates == [date(2026, 11, 9), date(2026, 11, 10)]


def test_same_store_and_date_cannot_start_a_second_sp_fetch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'warehouse.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    target = "2026-11-10"
    calls = []

    class Concurrent(FakeLingxingClient):
        def iter_advertising_report(self, report_type, payload):
            calls.append(payload["report_date"])
            return iter([{**row, "cost": 99}
                         for row in super().iter_advertising_report(report_type, payload)])

    class Earlier(FakeLingxingClient):
        def iter_advertising_report(self, report_type, payload):
            # A distinct connection attempts the same partition while the first
            # provider request is active. This models broad/hourly overlap.
            with sessions() as competing:
                with pytest.raises(SPPartitionBusy) as caught:
                    collect_ads_for_day(Concurrent(), competing, target, report_types=("sp",))
                assert caught.value.store_id == "101"
                assert caught.value.metric_date == date.fromisoformat(target)
                assert calls == []
                assert competing.scalar(select(DataCoverage)) is None
                collect_ads_for_day(Concurrent(), competing, "2026-11-09", report_types=("sp",))
            return super().iter_advertising_report(report_type, payload)

    with sessions() as db:
        collect_ads_for_day(Earlier(), db, target, report_types=("sp",))
    with sessions() as db:
        collect_ads_for_day(Concurrent(), db, target, report_types=("sp",))
        assert calls == ["2026-11-09", target]
        rows = db.scalars(select(DataCoverage).order_by(DataCoverage.metric_date)).all()
        assert [(row.metric_date.isoformat(), row.status) for row in rows] == [
            ("2026-11-09", "COMPLETE"), (target, "COMPLETE"),
        ]
        spend = db.scalar(select(DailyMetric.value).where(
            DailyMetric.metric_date == date.fromisoformat(target),
            DailyMetric.metric_name == "spend"))
        assert spend == 99
    engine.dispose()


@pytest.mark.parametrize("first_report", ["available", "empty", None])
def test_sp_failure_preserves_complete_or_records_missing_partition(tmp_path, first_report):
    engine = create_engine(f"sqlite:///{tmp_path / 'warehouse.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)

    class Empty(FakeLingxingClient):
        def iter_advertising_report(self, report_type, payload):
            return iter(())

    class Failed(FakeLingxingClient):
        def iter_advertising_report(self, report_type, payload):
            raise RuntimeError("Simulated provider error")

    target = "2026-11-10"
    saved = None
    if first_report is not None:
        with sessions() as db:
            client = FakeLingxingClient() if first_report == "available" else Empty()
            collect_ads_for_day(client, db, target, report_types=("sp",))
            row = db.scalar(select(DataCoverage))
            saved = (row.status, row.row_count, row.completed_at)

    with sessions() as db:
        with pytest.raises(RuntimeError, match="Simulated provider error"):
            collect_ads_for_day(Failed(), db, target, report_types=("sp",))
    with sessions() as db:
        row = db.scalar(select(DataCoverage))
        if first_report is not None:
            assert (row.status, row.row_count, row.completed_at) == saved
        else:
            assert row.status == "FAILED" and row.row_count == 0
        spend = db.scalar(select(DailyMetric).where(DailyMetric.metric_name == "spend"))
        assert (spend.value if spend is not None else None) == (12.5 if first_report == "available" else None)
    engine.dispose()


def test_sp_does_not_reconnect_and_write_if_pinned_connection_is_lost(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.exc import PendingRollbackError

    from jx_alert.lingxing import collector

    engine = create_engine(f"sqlite:///{tmp_path / 'warehouse.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    pinned = None

    @contextmanager
    def simulated_mysql_lock(engine, name, *, pin_connection):
        nonlocal pinned
        assert pin_connection and len(name) == 36
        with engine.connect() as pinned:
            try:
                yield pinned
            finally:
                assert pinned.invalidated  # No failed-coverage write reconnected it.

    class FailingConnection(FakeLingxingClient):
        def iter_advertising_report(self, report_type, payload):
            assert pinned is not None and pinned.in_transaction()
            rows = super().iter_advertising_report(report_type, payload)
            pinned.invalidate()
            return rows

    monkeypatch.setattr(collector, "database_job_lock", simulated_mysql_lock)
    with sessions() as db, pytest.raises(PendingRollbackError):
        collect_ads_for_day(FailingConnection(), db, "2026-11-10", report_types=("sp",))
    with sessions() as db:
        assert db.scalar(select(DataCoverage)) is None
        assert db.scalar(select(DailyMetric)) is None
    engine.dispose()
