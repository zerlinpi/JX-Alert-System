from datetime import date
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import DailyMetric, Product, RawSnapshot, Store
from jx_alert.lingxing.sales_collector import collect_sales_for_day, _fetch_sales_rows


class FakeSalesClient:
    def get_stores(self):
        return {"code": 0, "data": [{
            "sid": 101,
            "mid": 1,
            "name": "Store A",
            "seller_id": "seller-1",
            "account_name": "account-a",
            "region": "NA",
            "country": "United States",
            "marketplace_id": "ATVPDKIKX0DER",
            "status": 1,
            "has_ads_setting": 1,
        }]}

    def sales_daily(self, payload):
        metric_values = {1: 100.5, 2: 5, 3: 3}
        row = {
            "sid": 101,
            "r_date": payload["event_date"],
            "currency_code": "USD",
            "seller_sku": "SKU-1",
            "asin": "B000000001",
            "product_name": "Sample Product",
            "map_value": metric_values[payload["type"]],
        }
        return {"code": 0, "total": 1, "data": [row]}


def test_sales_collector_saves_store_asin_and_msku_metrics():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        result = collect_sales_for_day(FakeSalesClient(), db, "2026-11-10")
        assert result == {
            "stores_seen": 1,
            "active_stores_collected": 1,
            "stores_skipped_by_status": 0,
            "sales_rows": 6,
            "products_seen": 1,
        }
        assert db.scalar(select(Store)).name == "Store A"
        product = db.scalar(select(Product))
        assert product.seller_sku == "SKU-1"
        assert product.asin == "B000000001"
        assert db.scalar(select(func.count()).select_from(DailyMetric)) == 9
        store_sales = db.scalar(select(DailyMetric).where(
            DailyMetric.dimension == "store",
            DailyMetric.metric_name == "sales",
        ))
        assert store_sales.value == 100.5
        assert store_sales.currency == "USD"
        asin_sales = db.scalar(select(DailyMetric).where(
            DailyMetric.dimension == "sku_asin",
            DailyMetric.metric_name == "sales",
        ))
        assert asin_sales.entity_id == "B000000001"
        msku_orders = db.scalar(select(DailyMetric).where(
            DailyMetric.dimension == "sku_msku",
            DailyMetric.metric_name == "orders",
        ))
        assert msku_orders.value == 3
        assert store_sales.metric_date == date(2026, 11, 10)
        assert db.scalar(select(func.count()).select_from(RawSnapshot)) == 7


def test_sales_collector_does_not_synthesize_missing_metrics_as_zero():
    class MissingOrdersClient(FakeSalesClient):
        def sales_daily(self, payload):
            if payload["type"] == 3:
                return {"code": 0, "total": 0, "data": []}
            return super().sales_daily(payload)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        collect_sales_for_day(MissingOrdersClient(), db, "2026-11-10")
        orders = db.scalars(select(DailyMetric).where(
            DailyMetric.metric_name == "orders"
        )).all()
        assert orders == []


def test_sales_pagination_uses_offset_and_length():
    calls = []
    pages = [
        {"code": 0, "total": 3, "data": [{"id": 1}, {"id": 2}]},
        {"code": 0, "total": 3, "data": [{"id": 3}]},
    ]

    class Client:
        def sales_daily(self, payload):
            calls.append((payload["offset"], payload["length"]))
            return pages.pop(0)

    rows = _fetch_sales_rows(
        Client(),
        {"sid": 101, "event_date": "2026-11-10", "asin_type": 2, "type": 1},
        page_size=2,
    )
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert calls == [(0, 2), (2, 2)]
