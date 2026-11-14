from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from openpyxl import load_workbook
import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.reporting import daily_snapshot as snapshot
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AnomalyEvent, DailyMetric, DataCoverage, PerformanceNotice, RawSnapshot, Store,
    StorePerformance, SyncJob,
)
from jx_alert.storage.sp import CollectionLog, ReconciliationLog, SPDailyReport


ARCHIVE_DAY = date(2026, 11, 14)
REPORT_DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 14, 3, tzinfo=timezone.utc)
LONG_ID = "123456789012345678901234567890"
SECRET = "never-export-this-private-token"


@pytest.fixture
def warehouse(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite3"))
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Store(sid="5174", name='=HYPERLINK("https://example.invalid","店铺")',
            country="US", timezone="America/Los_Angeles", currency_code="USD", status=1))
        db.add_all([DailyMetric(store_id="5174", metric_date=REPORT_DAY, dimension="store",
            entity_id="5174", metric_name=metric, value=value, currency=currency)
            for metric, value, currency in [("orders", 0, None), ("sales", 100, "USD")]])
        db.add(DataCoverage(store_id="5174", metric_date=REPORT_DAY, dimension="store", status="COMPLETE", row_count=1))
        db.add(RawSnapshot(source="lingxing_sp", store_id="5174", payload={"access_token": SECRET}))
        db.add(StorePerformance(store_id="5174", pull_date=REPORT_DAY, ahr_score=256,
            ahr_status="GREAT", source_updated_at="2026-11-13 06:00:00", freshness_status="NOT_CONFIGURED",
            indicators={"order_with_defect": 0, "valid_tracking": None, "private_key": SECRET},
            standards={"order_with_defect": 1, "valid_tracking": 95}, detail={"token": SECRET}))
        db.add(PerformanceNotice(store_id="5174", notice_key="safe-fingerprint", subject=SECRET,
            payload={"token": SECRET}, status_label="PENDING"))
        db.add(SyncJob(job_type="collection", status="SUCCEEDED", metadata_json={"token": SECRET}, error_message=SECRET))
        db.add(AnomalyEvent(dedupe_key="x", store_id="5174", rule="high_spend_no_orders", severity="HIGH",
            reason=SECRET, metrics={"token": SECRET}))
        db.add(NotificationOutbox(idempotency_key="y", payload={"text": SECRET, "open_ids": [SECRET]},
            last_error=SECRET, chat_id=SECRET))
        for index, cost in enumerate((Decimal("1.123456789012"), Decimal("2.123456789012")), 1):
            run_id = f"run-{index}"
            db.add(CollectionLog(run_id=run_id, store_id="5174", campaign_id=LONG_ID, status="COMPLETE",
                started_at=NOW.replace(tzinfo=None), finished_at=NOW.replace(tzinfo=None),
                content_sha256=str(index) * 64, row_counts={"daily": 1, "ad_group": 3}, summary={"token": SECRET}))
            db.add(SPDailyReport(run_id=run_id, store_id="5174", profile_id=LONG_ID, campaign_id=LONG_ID,
                report_date=REPORT_DAY, row_key=str(index) * 64, captured_at=NOW.replace(tzinfo=None),
                source_snapshot_id=1, cost=cost, clicks=0, impressions=10, orders=0, sales=0,
                raw_json={"token": SECRET}, campaign_name="@external campaign"))
        db.add(ReconciliationLog(run_id="run-1", store_id="5174", campaign_id=LONG_ID, report_date=REPORT_DAY,
            scope="campaign", captured_at=NOW.replace(tzinfo=None), collection_status="COMPLETE",
            comparison_status="DIFFERENT", daily_metrics={"cost": 10, "private_key": SECRET},
            hourly_metrics={"cost": 9}, delta_hour_minus_daily={"cost": -1}))
        db.commit()
        settings = SimpleNamespace()
        yield db, settings
    engine.dispose()


def export(warehouse, tmp_path, **kwargs):
    db, settings = warehouse
    return snapshot.export_daily_snapshot(db, tmp_path / "excel", ARCHIVE_DAY,
        now=NOW, settings=settings, **kwargs)


def table(result, key):
    item = next(item for item in result["datasets"] if item["key"] == key)
    path = Path(result["manifest_path"]).parent / item["file"]
    rows = []
    with closing(load_workbook(path, read_only=True)) as workbook:
        for sheet in workbook:
            values = iter(sheet.values)
            headers = next(values)
            rows.extend(dict(zip(headers, (*row, *([None] * (len(headers) - len(row)))))) for row in values)
    return rows


def test_complete_snapshot_has_all_history_versions_unknowns_and_safe_source_indexes(warehouse, tmp_path):
    result = export(warehouse, tmp_path)
    assert result["status"] == "COMPLETE"
    directory = tmp_path / "excel" / "daily" / "2026" / "11" / "14"
    assert Path(result["manifest_path"]) == directory / "manifest.json"
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["snapshot_date"] == ARCHIVE_DAY.isoformat()
    assert manifest["archive_timezone"] == "Asia/Shanghai"
    assert {item["dimension"] for item in result["datasets"] if "dimension" in item} == set(snapshot.FACT_DIMENSIONS)
    assert all((directory / item["file"]).is_file() for item in result["datasets"])
    assert next(item for item in result["datasets"] if item["key"] == "sp_hourly")["state"] == "EMPTY"
    assert table(result, "raw_snapshots")[0]["快照ID"] == "1"
    versions = table(result, "sp_daily")
    assert [r["版本ID"] for r in versions] == ["run-1", "run-2"]
    assert all(r["活动ID"] == LONG_ID for r in versions)
    assert [r["花费精确十进制文本"] for r in versions] == ["1.123456789012", "2.123456789012"]
    assert all(r["报表日期"] == REPORT_DAY.isoformat() for r in versions)
    indicators = table(result, "health_indicators")
    assert len(indicators) == 7
    assert next(r for r in indicators if r["指标"] == "订单缺陷率")["观测百分数"] == 0
    assert next(r for r in indicators if r["指标"] == "有效追踪率")["观测百分数"] is None
    kpis = table(result, "advertising_kpis")
    assert len(kpis) == 7
    assert {r["实际报表日"] for r in kpis} == {REPORT_DAY.isoformat()}
    assert next(r for r in kpis if r["指标代码"] == "CTR")["指标实际归因日期"] == REPORT_DAY.isoformat()
    assert next(r for r in kpis if r["指标代码"] == "CPA")["指标实际归因日期"] == "2026-10-29"
    for path in result["files"]:
        with ZipFile(path) as archive:
            assert SECRET not in b"".join(archive.read(name) for name in archive.namelist()).decode("utf-8", errors="ignore")
    reconciled = next(r for r in table(result, "reconciliations") if r["指标"] == "cost")
    assert reconciled["小时减日报"] == -1
    assert next(r for r in table(result, "reconciliations") if r["指标"] == "orders")["日报数值"] is None
    counts = table(result, "collection_counts")
    assert next(r for r in counts if r["版本ID"] == "run-1" and r["范围"] == "ad_group")["源记录数"] == 3
    directory_rows = table(result, "00_归档目录")
    assert len(directory_rows) == len(result["datasets"]) - 1
    assert next(r for r in directory_rows if r["数据维度"] == "SP小时版本")["状态"] == "无已保存记录"


def test_streamed_workbook_preserves_literal_formula_names_and_long_ids(warehouse, tmp_path):
    result = export(warehouse, tmp_path)
    store_file = next(path for path in result["files"] if Path(path).stem == "stores")
    with closing(load_workbook(store_file, read_only=True)) as workbook:
        assert workbook.active["B2"].value.startswith("=HYPERLINK")
        assert workbook.active["B2"].data_type == "s"
    campaign_file = next(path for path in result["files"] if Path(path).stem == "sp_daily")
    with closing(load_workbook(campaign_file, read_only=True)) as workbook:
        assert workbook.active["E2"].value == LONG_ID
        assert workbook.active["E2"].data_type == "s"
        assert workbook.active["P2"].value == "@external campaign"
        assert workbook.active["P2"].data_type == "s"


def test_daily_success_is_immutable_and_repeated_call_cannot_accumulate_facts(warehouse, tmp_path):
    first = export(warehouse, tmp_path)
    original = Path(first["manifest_path"]).read_bytes()
    db, _ = warehouse
    db.add(DailyMetric(store_id="5174", metric_date=ARCHIVE_DAY, dimension="store", entity_id="5174",
        metric_name="orders", value=99))
    db.commit()
    second = export(warehouse, tmp_path)
    assert second["status"] == "ALREADY_COMPLETE"
    assert second["row_count"] == first["row_count"]
    assert Path(second["manifest_path"]).read_bytes() == original
    assert len(list(Path(first["manifest_path"]).parent.parent.iterdir())) == 1


def test_new_detail_workbooks_preserve_previous_archive_and_original_identifiers(warehouse, tmp_path, monkeypatch):
    with monkeypatch.context() as old_format:
        old_format.setattr(snapshot, "FACT_DIMENSIONS", snapshot._V1_FACT_DIMENSIONS)
        old_format.setattr(snapshot, "SCHEMA_VERSION", 1)
        original = export(warehouse, tmp_path)
    manifest_path = Path(original["manifest_path"])
    manifest_bytes = manifest_path.read_bytes()

    db, settings = warehouse
    detail_dimensions = {
        "creative_sb": "SB广告创意", "target_sb": "SB广告投放", "purchased_asin_sb": "SB归因购买商品",
        "target_sd": "SD商品定位", "purchased_asin_sd": "SD已购买商品",
    }
    numeric_dimensions = {"creative_sb", "target_sb", "target_sd"}
    identifiers = json.dumps([LONG_ID, "other-ASIN"], separators=(",", ":"))
    db.add_all(DailyMetric(store_id="5174", metric_date=REPORT_DAY, dimension=dimension,
        entity_id=identifiers, metric_name="spend", value=0, currency="USD") for dimension in numeric_dimensions)
    db.commit()

    assert export(warehouse, tmp_path)["status"] == "ALREADY_COMPLETE"
    assert manifest_path.read_bytes() == manifest_bytes
    updated = snapshot.export_daily_snapshot(db, tmp_path / "excel", date(2026, 11, 15), now=NOW, settings=settings)
    assert updated["status"] == "COMPLETE"
    assert json.loads(Path(updated["manifest_path"]).read_bytes())["schema_version"] == 2
    by_dimension = {item.get("dimension"): item for item in updated["datasets"]}
    for dimension, title in detail_dimensions.items():
        item = by_dimension[dimension]
        assert item["title"] == title
        rows = table(updated, item["key"])
        if dimension in numeric_dimensions:
            assert item["state"] == "PRESENT" and item["row_count"] == 1
            assert rows[0]["对象ID"] == identifiers and rows[0]["数值"] == 0 and rows[0]["币种"] == "USD"
        else:
            assert item["state"] == "EMPTY" and item["row_count"] == 0
            assert rows == []


def test_split_sheets_does_not_drop_duplicate_or_reorder_rows_and_discovers_new_dimensions(warehouse, tmp_path):
    db, _ = warehouse
    for index in range(5):
        db.add(DailyMetric(store_id="5174", metric_date=REPORT_DAY, dimension="custom/../dimension",
            entity_id=str(index), metric_name="custom_count", value=index))
    db.commit()
    result = export(warehouse, tmp_path, max_rows_per_sheet=2, batch_size=1)
    item = next(item for item in result["datasets"] if item.get("dimension") == "custom/../dimension")
    assert item["sheets"] == 3 and item["row_count"] == 5
    assert Path(item["file"]).name == item["file"]
    assert [row["数值"] for row in table(result, item["key"])] == list(range(5))


def test_failed_export_never_publishes_completion_and_next_attempt_can_succeed(warehouse, tmp_path, monkeypatch):
    original = snapshot._write_dataset
    def fail(*args, **kwargs):
        if args[1] == "coverage":
            raise OSError("disk full")
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshot, "_write_dataset", fail)
    with pytest.raises(OSError):
        export(warehouse, tmp_path)
    parent = tmp_path / "excel" / "daily" / "2026" / "11"
    assert list(parent.iterdir()) == []
    monkeypatch.setattr(snapshot, "_write_dataset", original)
    assert export(warehouse, tmp_path)["status"] == "COMPLETE"


def test_corrupt_file_is_preserved_and_entire_snapshot_is_repaired(warehouse, tmp_path):
    first = export(warehouse, tmp_path)
    broken = Path(first["files"][0])
    broken.write_bytes(b"corrupt")
    repaired = export(warehouse, tmp_path)
    assert repaired["status"] == "COMPLETE"
    assert broken.read_bytes() != b"corrupt"
    preserved = list(broken.parent.parent.glob("14.incomplete-*"))
    assert len(preserved) == 1
    assert (preserved[0] / broken.name).read_bytes() == b"corrupt"


def test_partial_completion_manifest_cannot_hide_missing_dimensions(warehouse, tmp_path):
    result = export(warehouse, tmp_path)
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["datasets"] = manifest["datasets"][:1]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restored = export(warehouse, tmp_path)
    assert restored["status"] == "COMPLETE"
    assert len(restored["datasets"]) == len(result["datasets"])


def test_snapshot_keeps_same_database_view_when_a_writer_updates_a_later_page(warehouse, tmp_path, monkeypatch):
    db, _ = warehouse
    original = snapshot._pages
    updated = False
    def concurrent_read(reader, model, fields, batch_size, where=()):
        nonlocal updated
        for row in original(reader, model, fields, batch_size, where):
            if model is DailyMetric and not updated:
                updated = True
                with Session(db.get_bind()) as writer:
                    writer.execute(update(DailyMetric).where(DailyMetric.metric_name == "sales").values(value=999))
                    writer.commit()
            yield row
    monkeypatch.setattr(snapshot, "_pages", concurrent_read)
    result = export(warehouse, tmp_path, batch_size=1)
    dimension = next(item for item in result["datasets"] if item.get("dimension") == "store")
    sales = next(row for row in table(result, dimension["key"]) if row["指标"] == "sales")
    assert updated and sales["数值"] == 100
    assert db.scalar(select(DailyMetric.value).where(DailyMetric.metric_name == "sales")) == 999


def test_uncommitted_business_writes_are_rejected_without_committing_them(warehouse, tmp_path):
    db, _ = warehouse
    db.add(Store(sid="new", name="new", status=1))
    with pytest.raises(ValueError, match="Commit business changes"):
        export(warehouse, tmp_path)
    assert db.new


def test_sp_scope_row_counts_cover_the_persistence_contract(warehouse, tmp_path):
    from jx_alert.lingxing.sp_persistence import SCOPE_MODELS
    result = export(warehouse, tmp_path)
    rows = table(result, "collection_counts")
    assert {row["范围"] for row in rows} == set(SCOPE_MODELS)
    assert [row["源记录数"] for row in rows if row["范围"] == "ad_group"] == [3, 3]


def test_unknown_store_timezone_uses_the_same_global_completed_day_as_collection(warehouse, tmp_path):
    db, settings = warehouse
    store = db.get(Store, "5174")
    store.timezone = None
    db.add(DataCoverage(store_id="5174", metric_date=date(2026, 11, 13), dimension="store",
        status="COMPLETE", row_count=0))
    db.commit()
    clock = NOW.replace(hour=14)  # UTC-12 is already Nov 14: its completed day is Nov 13.
    result = snapshot.export_daily_snapshot(db, tmp_path / "excel", ARCHIVE_DAY, now=clock, settings=settings)
    rows = table(result, "advertising_kpis")
    assert {row["实际报表日"] for row in rows} == {"2026-11-13"}
    assert all("未知店铺时区" in row["时区依据"] and "UTC-12" in row["时区依据"] for row in rows)
    assert db.get(Store, "5174").timezone is None  # Never persist UTC-12 as a store's timezone.
