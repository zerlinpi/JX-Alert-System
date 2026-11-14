"""One complete daily Excel snapshot of persisted, allowlisted business facts.

The snapshot day is a Beijing archive day, never a replacement report date.
Payloads, credentials, recipient identities and arbitrary error text are excluded.
Every historical fact/version remains a separate row; nothing is accumulated.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.cell import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.analysis.store_health import _indicator_rows
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.models import (
    AdProduct, AnomalyEvent, Campaign, DailyMetric, DataCoverage, NotificationLog,
    PerformanceNotice, PerformanceSyncState, Product, RawSnapshot, Store,
    StorePerformance, SyncJob,
)
from jx_alert.storage.sp import (
    CollectionLog, ReconciliationLog, SPAdvertiseDetail, SPDailyReport,
    SPHourlyReport, SPTargetDetail,
)


SCHEMA_VERSION = 2
EXCEL_DATA_ROWS = 1_048_575
_V1_FACT_DIMENSIONS = {
    "store": "店铺销量事实", "sku_msku": "MSKU销量事实", "sku_asin": "ASIN销量事实",
    "campaign_sp": "SP活动事实", "campaign_sb": "SB活动事实", "campaign_sd": "SD活动事实",
    "product_ad_sp": "SP商品广告事实", "product_ad_sd": "SD商品广告事实",
}
FACT_DIMENSIONS = {
    **_V1_FACT_DIMENSIONS,
    "creative_sb": "SB广告创意",
    "target_sb": "SB广告投放",
    "purchased_asin_sb": "SB归因购买商品",
    "target_sd": "SD商品定位",
    "purchased_asin_sd": "SD已购买商品",
}
SP_METRICS = ("cost", "clicks", "impressions", "orders", "sales")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HEADER_FILL = PatternFill("solid", fgColor="17365D")

# Explicit column projections prevent ORM loads from reading raw response JSON.
# Each tuple is (database field, Chinese display name).
BASE_DATASETS = (
    ("stores", "店铺信息", Store, (
        ("sid", "店铺ID"), ("name", "店铺"), ("country", "国家地区"), ("region", "区域"),
        ("marketplace_id", "市场ID"), ("timezone", "报表时区"), ("currency_code", "币种"),
        ("status", "源状态码"), ("has_ads_setting", "广告授权"), ("updated_at", "更新时间UTC"))),
    ("products", "商品信息", Product, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("seller_sku", "MSKU"), ("asin", "ASIN"),
        ("local_sku", "本地SKU"), ("product_name", "商品名称"), ("currency_code", "币种"),
        ("updated_at", "更新时间UTC"))),
    ("campaigns", "广告活动信息", Campaign, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("campaign_type", "广告类型"),
        ("campaign_id", "活动ID"), ("profile_id", "广告账户ID"), ("name", "活动名称"),
        ("state", "活动状态"), ("serving_status", "投放状态"), ("budget", "预算"),
        ("budget_type", "预算类型"), ("start_date", "开始日期"), ("end_date", "结束日期"),
        ("last_over_budget_at", "最近超预算时间"), ("updated_at", "更新时间UTC"))),
    ("ad_products", "广告商品信息", AdProduct, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("campaign_type", "广告类型"),
        ("ad_id", "广告ID"), ("campaign_id", "活动ID"), ("ad_group_id", "广告组ID"),
        ("asin", "ASIN"), ("seller_sku", "MSKU"), ("state", "状态"),
        ("serving_status", "投放状态"), ("updated_at", "更新时间UTC"))),
    ("coverage", "采集覆盖", DataCoverage, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("metric_date", "报表日期"),
        ("dimension", "维度"), ("status", "采集状态"), ("row_count", "源记录数"),
        ("completed_at", "更新时间UTC"))),
    ("performance", "绩效源观测", StorePerformance, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("pull_date", "拉取日期"),
        ("source_updated_at", "源更新时间及源时区待核实"), ("captured_at", "采集时间UTC"),
        ("ahr_score", "健康评分源观测"), ("ahr_status", "账户源评级"),
        ("freshness_status", "入库时数据质量状态"))),
    ("performance_notices", "绩效通知索引", PerformanceNotice, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("notice_key", "通知指纹"),
        ("status_code", "源状态码"), ("status_label", "通知状态"), ("occurred_at", "源发生时间"),
        ("is_read", "源已读标记"), ("source_updated_at", "源更新时间"),
        ("first_seen", "首次采集UTC"), ("last_seen", "最近采集UTC"))),
    ("performance_sync", "绩效采集状态", PerformanceSyncState, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("dataset", "数据集"), ("status", "状态"),
        ("source_updated_at", "源更新时间"), ("captured_at", "采集时间UTC"), ("record_count", "记录数"))),
    ("raw_snapshots", "原始响应索引", RawSnapshot, (
        ("id", "快照ID"), ("source", "来源"), ("store_id", "店铺ID"), ("captured_at", "采集时间UTC"))),
    ("collection_logs", "SP采集版本", CollectionLog, (
        ("run_id", "版本ID"), ("store_id", "店铺ID"), ("campaign_id", "活动ID"),
        ("source", "来源"), ("status", "状态"), ("started_at", "开始UTC"),
        ("finished_at", "结束UTC"), ("content_sha256", "内容指纹"))),
    ("sync_jobs", "采集任务记录", SyncJob, (
        ("id", "任务ID"), ("job_type", "任务类型"), ("status", "状态"),
        ("started_at", "开始UTC"), ("finished_at", "结束UTC"),
        ("store_count", "店铺数"), ("record_count", "记录数"))),
    ("anomalies", "异常记录", AnomalyEvent, (
        ("id", "记录ID"), ("store_id", "店铺ID"), ("rule", "规则"), ("entity_id", "对象ID"),
        ("severity", "等级"), ("status", "状态"), ("first_seen", "首次发现UTC"),
        ("last_seen", "最近发现UTC"), ("escalated_at", "升级UTC"), ("recovered_at", "恢复UTC"))),
    ("notification_logs", "通知投递记录", NotificationLog, (
        ("id", "记录ID"), ("channel", "通道"), ("status", "投递状态"), ("created_at", "记录时间UTC"))),
    ("notification_outbox", "通知队列状态", NotificationOutbox, (
        ("id", "记录ID"), ("status", "状态"), ("attempts", "尝试次数"),
        ("next_attempt_at", "下次尝试UTC"), ("claimed_at", "领取UTC"),
        ("delivered_at", "送达UTC"), ("created_at", "创建UTC"))),
)
_SP_COMMON = (
    ("id", "记录ID"), ("run_id", "版本ID"), ("store_id", "店铺ID"), ("profile_id", "广告账户ID"),
    ("campaign_id", "活动ID"), ("report_date", "报表日期"), ("row_key", "行指纹"),
    ("captured_at", "采集时间UTC"), ("source_snapshot_id", "源快照ID"), ("request_id", "请求追踪ID"),
    ("cost", "花费精确十进制文本"), ("clicks", "点击"), ("impressions", "曝光"),
    ("orders", "归因订单"), ("sales", "归因销售额精确十进制文本"),
)
SP_DATASETS = (
    ("sp_daily", "SP日报版本", SPDailyReport, (("campaign_name", "活动名称"),)),
    ("sp_hourly", "SP小时版本", SPHourlyReport, (("scope", "范围"), ("hour", "小时"), ("group_id", "广告组ID"))),
    ("sp_advertise", "SP广告商品明细版本", SPAdvertiseDetail, (
        ("hour", "小时"), ("ad_id", "广告ID"), ("group_id", "广告组ID"), ("asin", "ASIN"), ("seller_sku", "MSKU"))),
    ("sp_target", "SP投放明细版本", SPTargetDetail, (
        ("hour", "小时"), ("targeting_id", "投放ID"), ("group_id", "广告组ID"), ("ad_id", "广告ID"),
        ("asin", "ASIN"), ("seller_sku", "MSKU"), ("match_type", "匹配类型"), ("targeting", "投放表达式"))),
)
FACT_COLUMNS = (
    ("id", "事实ID"), ("store_id", "店铺ID"), ("metric_date", "报表日期"), ("dimension", "维度"),
    ("entity_id", "对象ID"), ("metric_name", "指标"), ("value", "数值"), ("currency", "币种"), ("source", "来源"),
)


def _cell_value(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return str(value) if abs(value) >= 10 ** 15 else value
    if isinstance(value, str):
        # Truncation would silently lose source data: fail and retry instead.
        cleaned = _CONTROL.sub("", value)
        if len(cleaned) > 32767:
            raise ValueError("Snapshot business text exceeds the Excel cell limit")
        return cleaned
    raise ValueError("Unapproved value type in Excel snapshot")


def _row_values(row, columns):
    return [str(row[key]) if row[key] is not None and (key == "id" or key == "sid" or key.endswith("_id"))
            else row[key] for key, _ in columns]


def _pages(db, model, fields, batch_size, where=()):
    primary = next(iter(model.__table__.primary_key.columns))
    high = db.scalar(select(func.max(primary)).where(*where))
    if high is None:
        return
    last = None
    while True:
        query = select(*(getattr(model, field) for field in fields)).where(primary <= high, *where)
        if last is not None:
            query = query.where(primary > last)
        rows = db.execute(query.order_by(primary).limit(batch_size)).mappings().all()
        if not rows:
            break
        yield from rows
        last = rows[-1][primary.name]


def _projected(db, model, columns, batch_size, where=()):
    for row in _pages(db, model, [key for key, _ in columns], batch_size, where):
        yield _row_values(row, columns)


@contextmanager
def _consistent_reader(db):
    if db.new or db.dirty or db.deleted:
        raise ValueError("Commit business changes before taking an Excel snapshot")
    with db.get_bind().connect() as connection:
        dialect = connection.dialect.name
        if dialect in {"mysql", "mariadb"}:
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        elif dialect != "sqlite":
            raise ValueError("Snapshot consistency supports MariaDB/MySQL and SQLite")
        with connection.begin():
            if dialect == "sqlite":
                # SQLite's legacy driver otherwise begins only for writes.
                connection.exec_driver_sql("BEGIN")
            with Session(bind=connection, autoflush=False) as reader:
                yield reader


def _write_dataset(directory, key, title, headers, rows, max_rows):
    filename = key + ".xlsx"
    path = directory / filename
    workbook = Workbook(write_only=True)
    sheet = None
    count = 0
    sheet_count = 0
    try:
        for row in rows:
            if len(row) != len(headers):
                raise ValueError("Snapshot row does not match its column schema")
            if sheet is None or count % max_rows == 0:
                sheet_count += 1
                sheet = _new_sheet(workbook, title, headers, sheet_count)
            cells = []
            for value in row:
                value = _cell_value(value)
                cell = WriteOnlyCell(sheet, value=value)
                if isinstance(value, str):
                    cell.data_type = "s"  # External names/IDs never become formulas.
                elif isinstance(value, (float, Decimal)):
                    cell.number_format = "0.############"
                cells.append(cell)
            sheet.append(cells)
            count += 1
        if sheet is None:
            sheet_count = 1
            _new_sheet(workbook, title, headers, sheet_count)
        workbook.save(path)
        os.chmod(path, 0o600)
    finally:
        workbook.close()
    return {"key": key, "title": title, "file": filename, "row_count": count,
            "sheets": sheet_count, "state": "EMPTY" if count == 0 else "PRESENT",
            "sha256": _sha256(path), "bytes": path.stat().st_size}


def _new_sheet(workbook, title, headers, number):
    sheet = workbook.create_sheet((title[:24] + f"_{number}")[:31])
    sheet.freeze_panes = "A2"
    for index in range(1, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 22
    cells = []
    for header in headers:
        cell = WriteOnlyCell(sheet, value=header)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = _HEADER_FILL
        cells.append(cell)
    sheet.append(cells)
    return sheet


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _performance_indicators(db, batch_size):
    fields = ["id", "store_id", "pull_date", "indicators", "standards"]
    for row in _pages(db, StorePerformance, fields, batch_size):
        indicators, _ = _indicator_rows(SimpleNamespace(indicators=row["indicators"], standards=row["standards"]))
        for indicator in indicators:
            yield [str(row["id"]), row["store_id"], row["pull_date"], indicator["label"],
                   indicator["value"], indicator["standard"], indicator["status_label"]]


def _reconciliations(db, batch_size):
    columns = ("id", "run_id", "store_id", "campaign_id", "report_date", "scope", "captured_at",
               "collection_status", "comparison_status", "row_count", "matched_row_count",
               "daily_metrics", "hourly_metrics", "delta_hour_minus_daily", "error_code")
    for row in _pages(db, ReconciliationLog, columns, batch_size):
        for metric in SP_METRICS:
            values = []
            for source in ("daily_metrics", "hourly_metrics", "delta_hour_minus_daily"):
                value = (row[source] or {}).get(metric) if isinstance(row[source], dict) else None
                values.append(value if isinstance(value, (float, int)) and not isinstance(value, bool) else None)
            yield [str(row["id"]), row["run_id"], row["store_id"], row["campaign_id"], row["report_date"],
                   row["scope"], row["captured_at"], row["collection_status"], row["comparison_status"],
                   row["row_count"], row["matched_row_count"], metric, *values, row["error_code"]]


def _collection_counts(db, batch_size):
    for row in _pages(db, CollectionLog, ["run_id", "store_id", "row_counts"], batch_size):
        counts = row["row_counts"] if isinstance(row["row_counts"], dict) else {}
        for scope in ("daily", "campaign", "ad_group", "ad", "target"):
            count = counts.get(scope)
            yield [row["run_id"], row["store_id"], scope,
                   count if type(count) is int and count >= 0 else None]


def _anomaly_metrics(db, batch_size):
    allowed = ("current", "previous", "baseline_7d", "change_ratio", "spend", "orders", "units",
               "sales", "clicks", "impressions", "spend_avg_7d", "orders_avg_7d", "spend_previous",
               "orders_previous", "daily_budget", "budget_ratio", "baseline", "threshold")
    for row in _pages(db, AnomalyEvent, ["id", "store_id", "metrics"], batch_size):
        metrics = row["metrics"] if isinstance(row["metrics"], dict) else {}
        for field in allowed:
            if field in metrics:
                value = metrics[field]
                yield [str(row["id"]), row["store_id"], field,
                       value if isinstance(value, (float, int)) and not isinstance(value, bool) else None]


def _kpi_rows(db, settings, clock, batch_size):
    fields = ["sid", "name", "timezone", "status"]
    for store in _pages(db, Store, fields, batch_size):
        try:
            zone = ZoneInfo(store["timezone"]) if store["timezone"] else None
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
        # An unknown marketplace cannot borrow another store's timezone.
        # The same UTC-12 mathematical lower bound used by store_report_plan;
        # it is not a claim about this marketplace's actual timezone.
        completed_zone = zone or timezone(timedelta(hours=-12))
        cutoff = clock.astimezone(completed_zone).date() - timedelta(days=1)
        latest = db.scalar(select(func.max(DataCoverage.metric_date)).where(
            DataCoverage.store_id == store["sid"], DataCoverage.metric_date <= cutoff,
            DataCoverage.dimension.in_(["store", "campaign_sp", "campaign_sb", "campaign_sd"]),
            DataCoverage.status == "COMPLETE"))
        target = latest or cutoff
        comparisons = advertising_kpis(db, settings, store["sid"], target)
        for code, entry in comparisons.items():
            authorized = store["status"] == 1
            yield [store["sid"], store["name"], target, "有完成采集证据" if latest else "尚无完成采集证据",
                   "店铺时区" if zone else "未知店铺时区，采用UTC-12全球已结束日下界", store["status"], code, entry["label"],
                   entry["current_date"], entry["current"] if authorized else None,
                   entry["baseline"] if authorized else None, entry["change_ratio"] if authorized else None,
                   entry["currency"], entry["status"] if authorized else "INSUFFICIENT_DATA",
                   entry["comparison_basis"], ",".join(entry["comparison_dates"]), entry["formula"],
                   entry["fingerprint"]]


def _manifest_result(directory, manifest, status):
    return {"status": status, "snapshot_date": manifest["snapshot_date"],
            "manifest_path": str(directory / "manifest.json"),
            "files": [str(directory / item["file"]) for item in manifest["datasets"]],
            "datasets": manifest["datasets"], "row_count": sum(item["row_count"] for item in manifest["datasets"])}


def _completed(directory, day):
    manifest_path = directory / "manifest.json"
    if directory.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Snapshot paths must not be symbolic links")
    if not manifest_path.exists():
        return None
    try:
        if manifest_path.stat().st_size > 1024 * 1024:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = manifest.get("schema_version")
        if (type(version) is not int or version not in {1, SCHEMA_VERSION} or manifest.get("status") != "COMPLETE"
                or manifest.get("snapshot_date") != day.isoformat() or not manifest.get("datasets")):
            return None
        required = {item[0] for item in BASE_DATASETS} | {item[0] for item in SP_DATASETS}
        required |= {"health_indicators", "reconciliations", "collection_counts", "anomaly_metrics",
                     "advertising_kpis", "00_归档目录"}
        # A completed pre-upgrade archive is immutable. Only new manifests
        # require the separate SB and SD detail workbooks.
        dimensions = _V1_FACT_DIMENSIONS if version == 1 else FACT_DIMENSIONS
        required |= {"facts_" + hashlib.sha256(dimension.encode()).hexdigest()[:16] for dimension in dimensions}
        keys = [item["key"] for item in manifest["datasets"]]
        if not required.issubset(keys) or len(keys) != len(set(keys)):
            return None
        for item in manifest["datasets"]:
            filename = item["file"]
            path = directory / filename
            if (Path(filename).name != filename or path.is_symlink() or not path.is_file()
                    or type(item["row_count"]) is not int or item["row_count"] < 0
                    or type(item["sheets"]) is not int or item["sheets"] < 1
                    or item["state"] not in {"EMPTY", "PRESENT"}
                    or _sha256(path) != item["sha256"]):
                return None
        return manifest
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def export_daily_snapshot(db, output_dir, snapshot_date, *, now=None, settings=None,
                          max_rows_per_sheet=EXCEL_DATA_ROWS, batch_size=1000):
    """Atomically publish one all-history, read-consistent snapshot per archive day.

    The caller holds the cross-process daily-snapshot job lock. A successful day
    is immutable. An incomplete/corrupt export is retained before repair.
    """
    day = date.fromisoformat(snapshot_date) if isinstance(snapshot_date, str) else snapshot_date
    if type(day) is not date:
        raise ValueError("Snapshot date must be a calendar date")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Daily snapshots require an aware clock")
    clock = clock.astimezone(timezone.utc)
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ValueError("Invalid snapshot page size")
    if type(max_rows_per_sheet) is not int or not 1 <= max_rows_per_sheet <= EXCEL_DATA_ROWS:
        raise ValueError("Invalid Excel sheet row limit")
    if settings is None:
        from jx_alert.config import get_settings
        settings = get_settings()
    root = Path(output_dir).resolve()
    parent = root / "daily" / day.strftime("%Y/%m")
    if not parent.resolve().is_relative_to(root):
        raise ValueError("Snapshot directories must remain inside the archive root")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = parent / day.strftime("%d")
    complete = _completed(directory, day)
    if complete:
        return _manifest_result(directory, complete, "ALREADY_COMPLETE")
    with tempfile.TemporaryDirectory(prefix=f".{day.day:02d}-", dir=parent) as temporary:
        staging = Path(temporary)
        os.chmod(staging, 0o700)
        datasets = []
        with _consistent_reader(db) as reader:
            for key, title, model, columns in BASE_DATASETS:
                datasets.append(_write_dataset(staging, key, title, [label for _, label in columns],
                    _projected(reader, model, columns, batch_size), max_rows_per_sheet))
            dimensions = dict(FACT_DIMENSIONS)
            for dimension in reader.scalars(select(DailyMetric.dimension).distinct().order_by(DailyMetric.dimension)):
                if dimension not in dimensions:
                    dimensions[dimension] = "其他标准化经营事实"
            for dimension, title in dimensions.items():
                key = "facts_" + hashlib.sha256(dimension.encode()).hexdigest()[:16]
                result = _write_dataset(staging, key, title, [label for _, label in FACT_COLUMNS],
                    _projected(reader, DailyMetric, FACT_COLUMNS, batch_size, (DailyMetric.dimension == dimension,)),
                    max_rows_per_sheet)
                result["dimension"] = dimension
                datasets.append(result)
            for key, title, model, extra in SP_DATASETS:
                columns = _SP_COMMON + extra
                datasets.append(_write_dataset(staging, key, title, [label for _, label in columns],
                    _projected(reader, model, columns, batch_size), max_rows_per_sheet))
            datasets.append(_write_dataset(staging, "health_indicators", "绩效七项历史观测",
                ["观测ID", "店铺ID", "拉取日期", "指标", "观测百分数", "平台标准百分数", "观测判定"],
                _performance_indicators(reader, batch_size), max_rows_per_sheet))
            datasets.append(_write_dataset(staging, "reconciliations", "SP逐项对账",
                ["记录ID", "版本ID", "店铺ID", "活动ID", "报表日期", "范围", "采集时间UTC", "采集状态",
                 "对账状态", "源记录数", "匹配记录数", "指标", "日报数值", "小时数值", "小时减日报", "错误码"],
                _reconciliations(reader, batch_size), max_rows_per_sheet))
            datasets.append(_write_dataset(staging, "collection_counts", "SP版本分维度行数",
                ["版本ID", "店铺ID", "范围", "源记录数"], _collection_counts(reader, batch_size), max_rows_per_sheet))
            datasets.append(_write_dataset(staging, "anomaly_metrics", "异常数值证据",
                ["异常记录ID", "店铺ID", "指标", "数值"], _anomaly_metrics(reader, batch_size), max_rows_per_sheet))
            datasets.append(_write_dataset(staging, "advertising_kpis", "各店最新报表七项指标",
                ["店铺ID", "店铺", "实际报表日", "报表日依据", "时区依据", "店铺源状态码", "指标代码", "指标",
                 "指标实际归因日期", "本期数值", "参考数值", "变化率", "币种", "判断", "参考口径", "参考日期",
                 "公式口径", "证据指纹"], _kpi_rows(reader, settings, clock, batch_size), max_rows_per_sheet))
        overview_rows = [[item["title"], item.get("dimension"), item["file"], item["row_count"],
                          "无已保存记录" if item["state"] == "EMPTY" else "已保存",
                          day.isoformat(), clock.isoformat()] for item in datasets]
        datasets.append(_write_dataset(staging, "00_归档目录", "归档目录",
            ["数据维度", "原始维度代码", "Excel文件", "记录数", "状态", "计划归档日北京时间", "实际读快照起始UTC"],
            overview_rows, max_rows_per_sheet))
        manifest = {"schema_version": SCHEMA_VERSION, "status": "COMPLETE", "snapshot_date": day.isoformat(),
            "archive_timezone": "Asia/Shanghai", "snapshot_started_at_utc": clock.isoformat(),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "all_persisted_business_history",
            "database_consistency": "repeatable_read", "datasets": datasets,
            "notes": ["快照日是北京时间计划归档日，报表与归因日期保留源值。",
                      "失败补做时导出补做时刻已保存的全库，不是恢复历史时点；实际读快照时点见snapshot_started_at_utc。",
                      "空白为缺失或不可比，真实零值保留0；EMPTY表示该维度尚无已保存记录。",
                      "SP各run_id为独立历史版本，不得将不同版本花费相加。",
                      "SP版本表没有独立币种字段，金额保留源数值，不从店铺默认币种推断。",
                      "原始响应仅导出索引，正文仍保存在数据库；不导出凭据、收件人身份或任意原始JSON。",
                      "绩效仅源观测，不能用未知源时区认定当前店铺健康。",
                      "高精度SP金额以十进制文本保存，避免Excel十五位有效数字截断。"]}
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        if directory.exists():
            os.replace(directory, parent / (day.strftime("%d") + ".incomplete-" + uuid4().hex))
        os.replace(staging, directory)
    return _manifest_result(directory, manifest, "COMPLETE")
