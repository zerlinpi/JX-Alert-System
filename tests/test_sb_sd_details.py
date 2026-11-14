from datetime import date
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.lingxing.ad_details import collect_ad_details, daily_detail_dates
from jx_alert.lingxing.client import LingxingError
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot


DAY = "2026-11-13"
STORE = {"sid": 5171, "status": 1, "has_ads_setting": 1}
SCOPE = {"data": [STORE, {"sid": 999, "status": 0, "has_ads_setting": 1}]}
CURRENCIES = {("5171", "901"): "USD"}
BASE = {"report_date": DAY, "profile_id": 901, "campaign_id": 100, "ad_group_id": 200}
TRAFFIC = {"cost": 0, "clicks": 0, "impressions": 17, "orders": 0, "sales": 0}


class DetailClient:
    def __init__(self, reports):
        self.reports = reports

    def iter_advertising_detail_report(self, dimension, payload):
        assert payload["sid"] == 5171 and payload["report_date"] == DAY
        assert payload.get("show_detail") == (1 if dimension == "purchased_asin_sd" else None)
        values = self.reports.get(dimension, [])
        if isinstance(values, Exception):
            raise values
        yield from values


def collect(db, reports):
    return collect_ad_details(DetailClient(reports), db, {"5171": [DAY]},
                              stores_response=SCOPE, ad_currencies=CURRENCIES)


def test_details_preserve_disjoint_grains_missing_metrics_and_confirmed_zero():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        reports = {
            "creative_sb": [{**BASE, **TRAFFIC, "ad_creative_id": "800000000000140764"}],
            "target_sb": [{**BASE, **TRAFFIC, "keyword_id": 700, "target_id": 0},
                          {**BASE, **TRAFFIC, "keyword_id": 0, "target_id": 700}],
            "purchased_asin_sb": [{**BASE, "campaign_id": None, "ad_group_id": None,
                "campaign_name": "Legacy Campaign", "asin": "B00SAMPLE1", "attribution_type": kind,
                "sales14d": 25, "orders14d": 1, "units_sold14d": 2,
                "new_to_brand_sales_percentage14d": 9} for kind in ("Promoted", "Brand Halo")],
            "purchased_asin_sd": [{**BASE, "tactic": "T00020", "asin": "B00SAMPLE1", "sku": "SKU1",
                "other_asin": "B00SAMPLE2", "currency": "USD", "other_units": 1, "other_sales": 10,
                "other_orders": None, "other_orders_14d": "1"}],
        }
        for _ in range(2):
            result = collect(db, reports)
            assert result["status"] == "COMPLETE"
            assert result["rows"] == {"creative_sb": 1, "target_sb": 2, "purchased_asin_sb": 2,
                                      "target_sd": 0, "purchased_asin_sd": 1}
        facts = db.scalars(select(DailyMetric)).all()
        assert len({(f.dimension, f.entity_id, f.metric_name) for f in facts}) == len(facts)
        assert all(not f.dimension.startswith("campaign_") for f in facts)
        creative = next(f for f in facts if f.dimension == "creative_sb" and f.metric_name == "spend")
        assert creative.value == 0 and creative.currency == "USD"
        assert json.loads(creative.entity_id)["creative"] == "800000000000140764"
        assert not any(f.dimension == "creative_sb" and f.metric_name == "units" for f in facts)
        assert not any(f.dimension == "purchased_asin_sd" and f.metric_name == "other_orders" for f in facts)
        assert next(f for f in facts if f.metric_name == "other_orders_14d").value == 1
        assert next(f for f in facts if f.metric_name == "new_to_brand_sales_percentage_14d").currency is None
        assert len({f.entity_id for f in facts if f.dimension == "target_sb"}) == 2
        assert len({f.entity_id for f in facts if f.dimension == "purchased_asin_sb"}) == 2
        assert len(db.scalars(select(RawSnapshot)).all()) == 10


def test_failed_refresh_preserves_old_facts_and_does_not_block_empty_or_other_reports():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        creative = {**BASE, **TRAFFIC, "ad_creative_id": 1}
        assert collect(db, {"creative_sb": [creative]})["status"] == "COMPLETE"
        result = collect(db, {"creative_sb": LingxingError("secret-bearing message"),
                              "target_sb": [{**BASE, **TRAFFIC, "keyword_id": 4, "report_date": "2026-11-12"}],
                              "target_sd": [{**BASE, **TRAFFIC, "target_id": 6}]})
        assert result["status"] == "PARTIAL_FAILED"
        assert "secret" not in json.dumps(result)
        coverage = {r.dimension: (r.status, r.row_count, r.metric_date) for r in db.scalars(select(DataCoverage))}
        assert coverage["creative_sb"] == ("FAILED", 1, date.fromisoformat(DAY))
        assert coverage["target_sb"][:2] == ("FAILED", 0)
        assert coverage["target_sd"][:2] == ("COMPLETE", 1)
        assert coverage["purchased_asin_sb"][:2] == ("COMPLETE", 0)
        assert db.scalar(select(DailyMetric).where(DailyMetric.dimension == "creative_sb")) is not None


def test_daily_supplemental_collection_keeps_attribution_checkpoint_and_latest_day():
    assert daily_detail_dates({"5171": ["2026-11-12", DAY]}) == {"5171": ["2026-10-30", DAY]}
