from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from contextlib import closing
import os
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from openpyxl import load_workbook
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.reporting import excel
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, Product, RawSnapshot, Store, SyncJob
from jx_alert.storage.sp import CollectionLog, ReconciliationLog, SPAdvertiseDetail, SPHourlyReport


DAY = date(2026, 11, 12)
CLOCK = datetime(2026, 11, 14, 0, tzinfo=timezone.utc)
LONG_ID = "123456789012345678901234567890"


@pytest.fixture
def warehouse():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid="5174", name='=HYPERLINK("https://example.invalid","x")', status=1,
            has_ads_setting=1, currency_code="CAD"))
        db.add(Campaign(store_id="5174", campaign_type="sp", campaign_id=LONG_ID,
            name="@external activity", state="enabled"))
        db.add(Product(store_id="5174", seller_sku="+external-sku", asin="B012345678"))
        for offset in range(22):
            day = DAY - timedelta(days=offset)
            for dimension in ("store", "sku_msku", "campaign_sp", "campaign_sb", "campaign_sd"):
                db.add(DataCoverage(store_id="5174", metric_date=day, dimension=dimension, status="COMPLETE",
                    row_count=1 if dimension in {"store", "campaign_sp", "sku_msku"} else 0))
            for dimension, entity in [("store", "5174"), ("sku_msku", "+external-sku"), ("campaign_sp", LONG_ID)]:
                for metric, value in {"spend": 100, "sales": 1000, "orders": 10, "units": 11,
                                      "clicks": 100, "impressions": 10000}.items():
                    if dimension != "campaign_sp" and metric not in {"sales", "orders", "units"}:
                        continue
                    if day == DAY and metric in {"orders", "spend"}:
                        value = 0
                    db.add(DailyMetric(store_id="5174", metric_date=day, dimension=dimension, entity_id=entity,
                        metric_name=metric, value=value, currency="USD" if metric in {"spend", "sales"} else None))
        db.flush()
        settings = SimpleNamespace(lingxing_performance_source_timezone="",
            lingxing_performance_observation_only=True, alert_kpi_change_ratio=0.15,
            alert_kpi_min_impressions=1000, alert_min_clicks=10, alert_min_spend=20)
        yield db, settings
    engine.dispose()


def data_rows(workbook, sheet):
    values = list(workbook[sheet].values)
    return [dict(zip(values[0], row)) for row in values[1:]]


def test_actual_xlsx_preserves_numeric_zero_missing_long_ids_and_literal_external_text(warehouse, tmp_path):
    db, settings = warehouse
    db.delete(db.scalar(select(DailyMetric).where(DailyMetric.metric_date == DAY,
        DailyMetric.dimension == "store", DailyMetric.metric_name == "units")))
    db.flush()
    latest, version = excel.archive_collected_store(db, "5174", DAY, tmp_path, settings=settings, now=CLOCK)
    assert latest.parent == tmp_path / "2026" / "11" / "12"
    assert latest.read_bytes() == version.read_bytes()
    with closing(load_workbook(latest, read_only=True)) as workbook:
        overview = data_rows(workbook, "店铺总览")[0]
        assert overview["订单"] == 0 and overview["销量"] is None
        assert overview["币种"] == "USD"  # Actual metric currency, not store default CAD.
        assert overview["数据完整性"] == "部分/缺失"
        assert workbook["店铺总览"]["B2"].data_type == "s"
        assert workbook["店铺总览"]["B2"].value.startswith("=HYPERLINK")
        assert workbook["广告活动"]["D2"].value == LONG_ID
        assert workbook["广告活动"]["D2"].data_type == "s"
        assert workbook["SKU销量"]["D2"].value == "+external-sku"
        assert workbook["SKU销量"]["D2"].data_type == "s"
        kpis = data_rows(workbook, "广告指标")
        assert {row["指标"] for row in kpis} == {"CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS"}
        assert next(row for row in kpis if row["指标"] == "CPA")["本期日期"] == "2026-10-29"
        assert next(row for row in kpis if row["指标"] == "CTR")["数值"] == 0.01
        assert workbook["广告指标"]["F2"].number_format == "0.00%"
        assert len(data_rows(workbook, "健康快报")) == 7
        assert data_rows(workbook, "健康快报")[0]["当前判定"] == "无绩效数据"
        assert all(row["日期"] == DAY.isoformat() for row in data_rows(workbook, "原始事实"))
        metadata = {row["项目"]: row["数值"] for row in data_rows(workbook, "归档信息")}
        assert metadata["导出时间（UTC）"] == CLOCK.isoformat()
        assert len(metadata["数据版本（SHA256）"]) == 64
    if os.name == "posix":
        assert latest.stat().st_mode & 0o777 == version.stat().st_mode & 0o777 == 0o600


def test_missing_entities_and_cross_currency_baselines_are_never_coerced_to_zero(warehouse):
    db, settings = warehouse
    missing_day = DAY - timedelta(days=3)
    db.delete(db.scalar(select(DailyMetric).where(DailyMetric.metric_date == missing_day,
        DailyMetric.dimension == "store", DailyMetric.metric_name == "orders")))
    previous = db.scalar(select(DailyMetric).where(DailyMetric.metric_date == DAY - timedelta(days=1),
        DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend"))
    previous.currency = "CAD"
    db.flush()
    rows = excel.build_daily_report_rows(db, DAY, store_id="5174", settings=settings, now=CLOCK)
    assert rows["店铺总览"][0][12] is None
    campaign = rows["广告活动"][0]
    assert campaign[16] is None and campaign[17] is None
    assert campaign[20] is None and campaign[21] is None
    record = db.scalar(select(DataCoverage).where(DataCoverage.metric_date == DAY,
        DataCoverage.dimension == "campaign_sp"))
    record.row_count = 2
    db.flush()
    rows = excel.build_daily_report_rows(db, DAY, store_id="5174", settings=settings, now=CLOCK)
    assert rows["店铺总览"][0][8] is None


def test_content_versions_are_idempotent_and_prior_revision_survives(warehouse, tmp_path):
    db, settings = warehouse
    latest, first = excel.archive_collected_store(db, "5174", DAY, tmp_path, settings=settings, now=CLOCK)
    old_bytes = first.read_bytes()
    repeated = excel.archive_collected_store(db, "5174", DAY, tmp_path, settings=settings,
                                             now=CLOCK + timedelta(minutes=5))
    assert repeated == [latest, first]
    assert first.read_bytes() == old_bytes
    metric = db.scalar(select(DailyMetric).where(DailyMetric.metric_date == DAY,
        DailyMetric.dimension == "store", DailyMetric.metric_name == "orders"))
    metric.value = 4
    db.flush()
    newer_latest, second = excel.archive_collected_store(db, "5174", DAY, tmp_path, settings=settings, now=CLOCK)
    assert second != first and newer_latest == latest
    assert first.read_bytes() == old_bytes
    with closing(load_workbook(second, read_only=True)) as workbook:
        assert data_rows(workbook, "店铺总览")[0]["订单"] == 4
    assert len(list(latest.parent.glob("*.xlsx"))) == 3


@pytest.mark.parametrize("sid", ["../escape", "1/../2", "1\\..\\2", True, 0, "-1"])
def test_archive_rejects_non_numeric_store_paths(warehouse, tmp_path, sid):
    db, settings = warehouse
    with pytest.raises(ValueError, match="positive numeric"):
        excel.archive_collected_store(db, sid, DAY, tmp_path, settings=settings, now=CLOCK)
    assert not list(tmp_path.iterdir())


def test_failed_atomic_save_retains_existing_file_and_removes_temporary(warehouse, tmp_path, monkeypatch):
    path = tmp_path / "existing.xlsx"
    path.write_bytes(b"known previous file")
    def broken_save(self, target):
        Path(target).write_bytes(b"partial new workbook")
        raise OSError("simulated disk error")
    monkeypatch.setattr(excel.Workbook, "save", broken_save)
    with pytest.raises(OSError):
        excel.export_daily_report(path)
    assert path.read_bytes() == b"known previous file"
    assert list(tmp_path.iterdir()) == [path]


def test_private_payloads_and_unrelated_raw_columns_are_not_exported(warehouse, tmp_path):
    db, settings = warehouse
    secret = "secret-fixture-must-not-appear"
    db.add(RawSnapshot(source="unrelated-private-response", store_id="5174",
        payload={"access_token": secret, "customer_email": "private@example.invalid"}))
    db.add(SyncJob(job_type="daily_collection", status="FAILED", started_at=CLOCK.replace(tzinfo=None),
        error_message=secret, metadata_json={"error_type": "AuthError", "access_token": secret}))
    db.flush()
    latest, _ = excel.archive_collected_store(db, "5174", DAY, tmp_path, settings=settings, now=CLOCK)
    with ZipFile(latest) as archive:
        payload = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".xml"))
    assert secret.encode() not in payload and b"private@example.invalid" not in payload


def test_sp_archive_keeps_run_versions_original_delta_and_whitelisted_details(warehouse, tmp_path):
    db, settings = warehouse
    for index, run_id in enumerate(("run-one", "run-two"), 1):
        db.add(CollectionLog(run_id=run_id, store_id="5174", campaign_id=LONG_ID, status="COMPLETE",
            started_at=CLOCK.replace(tzinfo=None), finished_at=CLOCK.replace(tzinfo=None),
            content_sha256=str(index) * 64, row_counts={}, summary={"access_token": "secret-sp-fixture"}))
        db.flush()
        common = dict(run_id=run_id, store_id="5174", profile_id="profile", campaign_id=LONG_ID,
            report_date=DAY, row_key=str(index) * 64, captured_at=CLOCK.replace(tzinfo=None),
            source_snapshot_id=index, cost=Decimal("87.72"), clicks=10, impressions=100, orders=1,
            sales=Decimal("120.00"), raw_json={"access_token": "secret-sp-fixture", "customer": "private"})
        db.add(SPHourlyReport(**common, scope="campaign", hour=12))
        db.add(SPAdvertiseDetail(**common, hour=12, ad_id=LONG_ID, asin="B012345678", seller_sku="=outside"))
        db.add(ReconciliationLog(run_id=run_id, store_id="5174", campaign_id=LONG_ID, report_date=DAY,
            scope="campaign", captured_at=CLOCK.replace(tzinfo=None), collection_status="COMPLETE",
            comparison_status="DIFFERENT", row_count=1, matched_row_count=1,
            daily_metrics={"cost": "93.20"}, hourly_metrics={"cost": "87.72"},
            delta_hour_minus_daily={"cost": "-5.48"}))
    db.flush()
    first = excel.archive_sp_run(db, "run-one", tmp_path)[0]
    second = excel.archive_sp_run(db, "run-two", tmp_path)[0]
    assert first != second
    assert excel.archive_sp_run(db, "run-one", tmp_path) == [first]
    with closing(load_workbook(first, read_only=True)) as workbook:
        observations = data_rows(workbook, "SP小时观测")
        assert len(observations) == 1 and observations[0]["版本ID"] == "run-one"
        assert observations[0]["活动ID"] == LONG_ID and observations[0]["花费"] == 87.72
        assert data_rows(workbook, "SP广告商品明细")[0]["MSKU"] == "=outside"
        cost = next(r for r in data_rows(workbook, "SP对账") if r["指标"] == "cost")
        assert cost["小时减日报"] == -5.48 and cost["对账状态"] == "DIFFERENT"
    with ZipFile(first) as archive:
        payload = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".xml"))
    assert b"secret-sp-fixture" not in payload and b"raw_json" not in payload
