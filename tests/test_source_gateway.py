"""Two source failures and independent reporting bases must remain distinguishable."""
import json

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from jx_alert.config import Settings
from jx_alert.lingxing.client import LingxingError
from jx_alert.lingxing import source_gateway as gateway
from jx_alert.lingxing.pipeline import collect_daily_snapshot
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot, Store


DAY = "2026-11-17"


@pytest.fixture
def warehouse(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, store_controls_file=str(tmp_path / "switches.json"),
        lingxing_business_credentials_file=str(tmp_path / "not-configured.json"),
        raw_dir=str(tmp_path / "raw"), lingxing_performance_enabled=False,
        sb_sd_detail_reports_enabled=False, excel_archive_enabled=False,
        feishu_quiet_delivery_enabled=True)
    with Session(engine) as db:
        db.add_all([Store(sid=str(sid), name=f"Store{sid}", status=1, has_ads_setting=1)
                    for sid in (101, 102)])
        db.commit()
        yield db, settings
    engine.dispose()


class Primary:
    fail_sid = None

    def get_stores(self):
        return {"data": [{"sid": sid, "name": f"Store{sid}", "status": 1, "has_ads_setting": 1}
                         for sid in (101, 102)]}

    def sales_daily(self, payload):
        if str(payload["sid"]) == self.fail_sid:
            raise LingxingError("Primary unavailable")
        return {"total": 1, "data": [{"r_date": payload["event_date"], "asin": "ASIN", "seller_sku": "SKU",
            "currency_code": "USD", "map_value": {1: 100, 2: 2, 3: 1}[payload["type"]]}]}

    def seller_ad_accounts(self):
        return [{"sid": str(sid), "profile_id": "7", "type": "seller", "currency_code": "USD"} for sid in (101, 102)]

    def iter_advertising_campaigns(self, *args, **kwargs):
        return iter([])

    def iter_advertising_report(self, *args, **kwargs):
        return iter([])

    def iter_advertising_product_report(self, *args, **kwargs):
        return iter([])


class Secondary:
    fail = False

    def __init__(self):
        self.sales_calls = 0

    def stores(self):
        if self.fail:
            raise LingxingError("Secondary unavailable")
        return [{"id": 101}, {"id": 102}, {"id": 999}], {"code": 1}

    def sales(self, sid, day, grain):
        self.sales_calls += 1
        if self.fail:
            raise LingxingError("Secondary unavailable")
        return [{"entity_id": "ASIN" if grain == "asin" else "SKU", "sales": 200,
                 "units": 4, "orders": 3, "currency": "USD"}], {"observed": True}

    def campaign_ads(self, sid, day, kind):
        if self.fail:
            raise LingxingError("Secondary unavailable")
        return [{"entity_id": "99", "profile_id": "7", "name": "Campaign", "spend": 5,
                 "clicks": 2, "impressions": 10}], {"observed": True}


def test_secondary_inventory_failure_does_not_break_successful_primary(warehouse):
    db, settings = warehouse
    reader = Secondary()
    reader.fail = True
    client = gateway.CollectionSources(Primary(), settings, db=db, reader=reader)
    assert len(client.get_stores()["data"]) == 2
    assert client.pending[0]["status"] == "FAILED"


def test_mcp_inventory_fallback_preserves_cached_authorization_and_excludes_new_ids(warehouse):
    db, settings = warehouse
    db.get(Store, "102").status = 2
    db.commit()
    client = gateway.CollectionSources(None, settings, db=db, reader=Secondary())
    rows = client.get_stores()["data"]
    assert [(r["sid"], r["status"]) for r in rows] == [(101, 1), (102, 2)]


def test_failed_secondary_cache_stays_failed_for_all_sales_types(warehouse):
    db, settings = warehouse
    reader = Secondary()
    reader.fail = True
    client = gateway.CollectionSources(Primary(), settings, db=db, reader=reader)
    for kind in (1, 2, 3):
        client.sales_daily({"sid": 101, "event_date": DAY, "asin_type": 1, "type": kind})
    assert reader.sales_calls == 1
    assert [v["mcp"] for v in client.outcomes] == ["FAILED"] * 3


def test_purchase_observations_remain_independent_and_are_not_added_to_paid_sales(warehouse):
    db, settings = warehouse
    reader = Secondary()
    client = gateway.CollectionSources(Primary(), settings, db=db, reader=reader)
    response = client.sales_daily({"sid": 101, "event_date": DAY, "asin_type": 1, "type": 1})
    assert response["data"][0]["map_value"] == 100
    client.flush(db)
    packet = gateway.source_observations(db, "101", DAY)
    assert packet["store_sales"]["metrics"]["sales"] == 200
    assert "下单口径" in packet["store_sales"]["basis"]
    assert {r.dimension for r in db.scalars(select(DailyMetric))} == {"mcp_sales_asin"}


def test_intraday_flush_writes_only_raw_and_never_daily_coverage(warehouse):
    db, settings = warehouse
    client = gateway.CollectionSources(Primary(), settings, db=db, reader=Secondary(), observation_mode=True)
    client.observe_campaign("101", DAY, "sp")
    client.flush(db, raw_only=True)
    assert db.scalar(select(func.count()).select_from(RawSnapshot)) > 0
    assert db.scalar(select(func.count()).select_from(DailyMetric)) == 0
    assert db.scalar(select(func.count()).select_from(DataCoverage)) == 0


def test_primary_failure_does_not_discard_secondary_or_successful_other_store(warehouse, monkeypatch):
    from jx_alert.analysis import service
    db, settings = warehouse
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    primary = Primary()
    primary.fail_sid = "102"
    client = gateway.CollectionSources(primary, settings, db=db, reader=Secondary())
    result = collect_daily_snapshot(client, db, DAY, settings=settings, ad_attribution_refresh_days=1)
    assert result["status"] == "PARTIAL_FAILED"
    by_key = {(r.store_id, r.dimension): r.status for r in db.scalars(select(DataCoverage))}
    assert by_key["101", "store"] == "COMPLETE"
    assert by_key["102", "store"] == "FAILED"
    assert by_key["102", "mcp_sales_asin"] == "COMPLETE"
    assert gateway.source_observations(db, "102", DAY)["store_sales"]["metrics"]["sales"] == 200
    paid = db.scalar(select(DailyMetric.value).where(DailyMetric.store_id == "101",
        DailyMetric.dimension == "store", DailyMetric.metric_name == "sales"))
    assert paid == 100
    assert "secret" not in json.dumps(result).lower()


def test_cached_store_identity_cannot_recover_authorization_events(warehouse, monkeypatch):
    from jx_alert.lingxing import pipeline
    db, settings = warehouse
    client = gateway.CollectionSources(Primary(), settings, db=db, reader=Secondary())
    inventory = gateway.CollectionSources(None, settings, db=db, reader=Secondary()).get_stores()
    def reject_cached_evaluation(*args):
        pytest.fail("Cached status is not fresh authorization evidence")
    monkeypatch.setattr(pipeline, "evaluate_store_status_events", reject_cached_evaluation)
    result = collect_daily_snapshot(client, db, DAY, stores_response=inventory,
        settings=settings, ad_attribution_refresh_days=1)
    assert result["steps"]["store_status"] == {"status": "SKIPPED", "reason": "NO_FRESH_AUTHORIZATION_EVIDENCE"}


def test_profile_lookup_failure_does_not_block_available_primary_ad_reports(warehouse):
    from jx_alert.lingxing.mcp_read_source import MCPReadError
    db, settings = warehouse
    class AccountsUnavailable(Primary):
        def seller_ad_accounts(self):
            raise LingxingError("Primary profiles unavailable")
    class ProfilesUnavailable(Secondary):
        def profiles(self):
            raise MCPReadError("Secondary profiles unavailable")
    client = gateway.CollectionSources(AccountsUnavailable(), settings, db=db, reader=ProfilesUnavailable())
    result = collect_daily_snapshot(client, db, DAY, settings=settings, ad_attribution_refresh_days=1)
    assert result["steps"]["ad_currency"]["status"] == "LOOKUP_UNAVAILABLE"
    assert all(result["steps"]["campaign_reports"]["by_type"][kind].get("status") != "PARTIAL_FAILED"
               for kind in ("sp", "sb", "sd"))
    covered = db.scalar(select(DataCoverage).where(DataCoverage.store_id == "101",
        DataCoverage.dimension == "campaign_sp"))
    assert covered.status == "COMPLETE" and covered.row_count == 0
