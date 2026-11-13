from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from jx_alert.notifications.daily_report import build_daily_card
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store


def test_advertising_never_inherits_sales_currency_or_compares_different_currencies():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    day = date(2026, 11, 11)
    with Session(engine) as db:
        store = Store(sid="1", name="跨币种店铺", currency_code="USD")
        db.add(store)
        for on, currency in [(day, None), (day - timedelta(days=1), "USD")]:
            db.add(DataCoverage(store_id="1", metric_date=on, dimension="campaign_sp", status="COMPLETE"))
            db.add(DailyMetric(store_id="1", metric_date=on, dimension="campaign_sp", entity_id="ad",
                               metric_name="spend", value=12, currency=currency))
        db.flush()
        def spend():
            value = build_daily_card(db, store, day)
            return next(e for e in value["body"]["elements"] if e["tag"] == "table")["rows"][3]
        assert spend()["c1"] == "未知（币种待确认）"
        assert spend()["c3"] == "币种待确认"
        row = db.query(DailyMetric).filter_by(metric_date=day).one()
        row.currency = "CAD"
        db.flush()
        assert spend()["c1"] == "加元 12.00"
        assert spend()["c2"] == "美元 12.00"
        assert spend()["c3"] == "币种不一致"
        row.currency = "USD"
        db.flush()
        assert spend()["c3"] == "+0.0%"
    engine.dispose()
