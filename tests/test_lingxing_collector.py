from datetime import date
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import DailyMetric, RawSnapshot
from jx_alert.lingxing.collector import collect_ads_for_day


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
        assert payload["report_date"] == "2026-11-10"
        return iter([{
            "campaign_id": 9001,
            "report_date": "2026-11-10",
            "cost": 12.5,
            "orders": 2,
            "sales": 50,
            "units": 2,
            "clicks": 10,
            "impressions": 1000,
        }])


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
            "rows": {"sp": 1, "sb": 1, "sd": 1},
        }
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 18
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 4
        spend = db.scalar(select(DailyMetric).where(DailyMetric.metric_name == "spend"))
        assert spend.value == 12.5
        assert spend.metric_date == date(2026, 11, 10)


def test_collect_ads_replaces_same_day_metrics_idempotently():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        client = FakeLingxingClient()
        collect_ads_for_day(client, db, "2026-11-10")
        collect_ads_for_day(client, db, "2026-11-10")
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 18



def test_collect_ads_accepts_store_local_date_map():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_ads_for_day(
            FakeLingxingClient(), db, {"101": "2026-11-09"}
        )
        assert result["report_date"] == "per-store mapping"
        metric = db.scalar(select(DailyMetric))
        assert metric.metric_date == date(2026, 11, 9)
