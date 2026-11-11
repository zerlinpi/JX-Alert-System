import json
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from jx_alert.storage.models import (
    AdProduct,
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    Product,
    Store,
    SyncJob,
)


SHEETS = {
    "店铺总览": [
        "店铺ID", "店铺", "国家/地区", "日期", "订单", "销量", "销售额",
        "币种", "广告花费", "广告订单",
        "前一日订单", "订单较前一日变化率", "前七日订单均值", "订单较七日均值变化率",
        "前一日销售额", "销售额较前一日变化率", "前七日销售额均值", "销售额较七日均值变化率",
        "前一日广告花费", "广告花费较前一日变化率", "前七日广告花费均值", "广告花费较七日均值变化率",
        "前一日广告订单", "广告订单较前一日变化率", "前七日广告订单均值", "广告订单较七日均值变化率",
        "数据完整性",
    ],
    "店铺绩效": [
        "店铺ID", "店铺", "领星店铺状态码", "广告授权", "账户健康评分",
        "绩效事件状态", "说明",
    ],
    "SKU销量": [
        "店铺ID", "店铺", "ASIN", "MSKU", "Local SKU", "日期", "订单",
        "销量", "销售额", "前一日订单", "订单较前一日变化率",
        "前一日销售额", "销售额较前一日变化率",
        "前七日订单均值", "订单较七日均值变化率",
        "前七日销售额均值", "销售额较七日均值变化率", "数据完整性",
    ],
    "广告活动": [
        "店铺ID", "店铺", "广告类型", "活动ID", "活动名称", "活动状态",
        "预算类型", "预算", "日期", "花费", "订单", "销售额", "ACOS",
        "ROAS", "点击", "曝光",
        "前一日花费", "花费较前一日变化率", "前一日广告订单", "广告订单较前一日变化率",
        "前七日花费均值", "花费较七日均值变化率", "前七日广告订单均值",
        "广告订单较七日均值变化率", "最近超预算时间",
    ],
    "广告商品": [
        "店铺ID", "店铺", "广告类型", "广告ID", "活动ID", "ASIN", "MSKU",
        "日期", "花费", "订单", "销售额", "ACOS", "ROAS", "点击", "曝光",
        "前一日花费", "花费较前一日变化率", "前一日广告订单", "广告订单较前一日变化率",
        "前七日花费均值", "花费较七日均值变化率", "前七日广告订单均值",
        "广告订单较七日均值变化率",
    ],
    "异常记录": [
        "等级", "店铺ID", "店铺", "对象", "规则", "状态", "首次发现",
        "最近发现", "升级时间", "恢复时间", "原因", "指标",
    ],
    "采集日志": [
        "任务ID", "任务", "开始时间", "结束时间", "状态", "店铺数",
        "记录数", "错误",
    ],
}


def _ratio(spend, sales):
    if spend is None or sales is None or sales <= 0:
        return None
    return spend / sales


def _roas(spend, sales):
    if spend is None or spend <= 0 or sales is None:
        return None
    return sales / spend


def _change_ratio(current, reference):
    if current is None or reference is None or reference <= 0:
        return None
    return (current - reference) / reference


def _iso(value):
    return value.isoformat(sep=" ") if hasattr(value, "isoformat") else value


def build_daily_report_rows(db, report_date: date | str, *,
                            store_id: str | None = None) -> dict[str, list[list]]:
    target = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    history_dates = [target - timedelta(days=offset) for offset in range(7, -1, -1)]
    stores_query = select(Store).order_by(Store.name)
    if store_id:
        stores_query = stores_query.where(Store.sid == str(store_id))
    stores = db.scalars(stores_query).all()
    store_map = {store.sid: store for store in stores}
    selected_ids = set(store_map)

    metrics = db.scalars(select(DailyMetric).where(
        DailyMetric.store_id.in_(selected_ids) if selected_ids else DailyMetric.store_id == "__none__",
        DailyMetric.metric_date.in_(history_dates),
    )).all()
    metric_map = {
        (row.store_id, row.metric_date, row.dimension, row.entity_id, row.metric_name):
            float(row.value)
        for row in metrics
    }
    coverage_rows = db.scalars(select(DataCoverage).where(
        DataCoverage.store_id.in_(selected_ids) if selected_ids else DataCoverage.store_id == "__none__",
        DataCoverage.metric_date.in_(history_dates),
        DataCoverage.status == "COMPLETE",
    )).all()
    coverage = {
        (row.store_id, row.metric_date, row.dimension) for row in coverage_rows
    }

    def complete(sid, dimension, day):
        return (sid, day, dimension) in coverage

    def value(sid, day, dimension, entity, metric):
        key = (sid, day, dimension, str(entity), metric)
        if key in metric_map:
            return metric_map[key]
        if complete(sid, dimension, day):
            return 0.0
        return None

    rows = {name: [] for name in SHEETS}

    campaigns_query = select(Campaign)
    products_query = select(Product)
    ads_query = select(AdProduct)
    anomalies_query = select(AnomalyEvent)
    if store_id:
        campaigns_query = campaigns_query.where(Campaign.store_id == str(store_id))
        products_query = products_query.where(Product.store_id == str(store_id))
        ads_query = ads_query.where(AdProduct.store_id == str(store_id))
        anomalies_query = anomalies_query.where(AnomalyEvent.store_id == str(store_id))
    campaigns = db.scalars(campaigns_query).all()
    products = db.scalars(products_query).all()
    advertised_products = db.scalars(ads_query).all()
    anomalies = db.scalars(
        anomalies_query.order_by(AnomalyEvent.last_seen.desc())
    ).all()

    campaign_by_store = {}
    for campaign in campaigns:
        campaign_by_store.setdefault(campaign.store_id, []).append(campaign)

    for store in stores:
        sid = store.sid
        sales_complete = complete(sid, "store", target)
        store_orders = value(sid, target, "store", sid, "orders")
        store_units = value(sid, target, "store", sid, "units")
        store_sales = value(sid, target, "store", sid, "sales")

        ad_spend = 0.0
        ad_orders = 0.0
        ad_complete = bool(store.has_ads_setting)
        if store.has_ads_setting:
            for campaign_type in ("sp", "sb", "sd"):
                dimension = f"campaign_{campaign_type}"
                if not complete(sid, dimension, target):
                    ad_complete = False
                    continue
                for campaign in campaign_by_store.get(sid, []):
                    if campaign.campaign_type != campaign_type:
                        continue
                    ad_spend += value(sid, target, dimension, campaign.campaign_id, "spend") or 0.0
                    ad_orders += value(sid, target, dimension, campaign.campaign_id, "orders") or 0.0
        else:
            ad_spend = None
            ad_orders = None

        rows["店铺总览"].append([
            sid, store.name, store.country or store.region, target.isoformat(),
            store_orders, store_units, store_sales, store.currency_code,
            ad_spend if ad_complete else None,
            ad_orders if ad_complete else None,
            "完整" if sales_complete and (ad_complete or not store.has_ads_setting) else "部分/缺失",
        ])
        rows["店铺绩效"].append([
            sid, store.name, store.status,
            "已配置" if store.has_ads_setting else "未配置",
            None, "未接入", "领星账户健康评分及绩效通知接口尚未接入",
        ])

        store_products = [product for product in products if product.store_id == sid]
        msku_coverage = all(
            complete(sid, "sku_msku", day) for day in history_dates
        )
        baseline_dates = history_dates[:-1]
        for product in store_products:
            current_orders = value(sid, target, "sku_msku", product.seller_sku, "orders")
            current_units = value(sid, target, "sku_msku", product.seller_sku, "units")
            current_sales = value(sid, target, "sku_msku", product.seller_sku, "sales")
            if msku_coverage:
                baseline_orders = [
                    value(sid, day, "sku_msku", product.seller_sku, "orders") or 0.0
                    for day in baseline_dates
                ]
                baseline_sales = [
                    value(sid, day, "sku_msku", product.seller_sku, "sales") or 0.0
                    for day in baseline_dates
                ]
                average_orders = sum(baseline_orders) / 7
                average_sales = sum(baseline_sales) / 7
            else:
                average_orders = None
                average_sales = None
            order_change_ratio = (
                (current_orders - average_orders) / average_orders
                if current_orders is not None and average_orders is not None and average_orders > 0
                else None
            )
            sales_change_ratio = (
                (current_sales - average_sales) / average_sales
                if current_sales is not None and average_sales is not None and average_sales > 0
                else None
            )
            rows["SKU销量"].append([
                sid, store.name, product.asin, product.seller_sku, product.local_sku,
                target.isoformat(), current_orders, current_units, current_sales,
                average_orders, order_change_ratio, average_sales, sales_change_ratio,
                "完整" if msku_coverage else "样本不足",
            ])

    for campaign in campaigns:
        store = store_map.get(campaign.store_id)
        if store is None:
            continue
        dimension = f"campaign_{campaign.campaign_type}"
        spend = value(campaign.store_id, target, dimension, campaign.campaign_id, "spend")
        orders = value(campaign.store_id, target, dimension, campaign.campaign_id, "orders")
        sales = value(campaign.store_id, target, dimension, campaign.campaign_id, "sales")
        clicks = value(campaign.store_id, target, dimension, campaign.campaign_id, "clicks")
        impressions = value(campaign.store_id, target, dimension, campaign.campaign_id, "impressions")
        rows["广告活动"].append([
            campaign.store_id, store.name, campaign.campaign_type.upper(),
            campaign.campaign_id, campaign.name, campaign.state, campaign.budget_type,
            campaign.budget, target.isoformat(), spend, orders, sales,
            _ratio(spend, sales), _roas(spend, sales), clicks, impressions,
            campaign.last_over_budget_at,
        ])

    for ad in advertised_products:
        store = store_map.get(ad.store_id)
        if store is None:
            continue
        dimension = f"product_ad_{ad.campaign_type}"
        spend = value(ad.store_id, target, dimension, ad.ad_id, "spend")
        orders = value(ad.store_id, target, dimension, ad.ad_id, "orders")
        sales = value(ad.store_id, target, dimension, ad.ad_id, "sales")
        clicks = value(ad.store_id, target, dimension, ad.ad_id, "clicks")
        impressions = value(ad.store_id, target, dimension, ad.ad_id, "impressions")
        rows["广告商品"].append([
            ad.store_id, store.name, ad.campaign_type.upper(), ad.ad_id,
            ad.campaign_id, ad.asin, ad.seller_sku, target.isoformat(),
            spend, orders, sales, _ratio(spend, sales), _roas(spend, sales),
            clicks, impressions,
        ])

    for anomaly in anomalies:
        store = store_map.get(anomaly.store_id)
        rows["异常记录"].append([
            anomaly.severity, anomaly.store_id, store.name if store else "",
            anomaly.entity_id, anomaly.rule, anomaly.status,
            _iso(anomaly.first_seen), _iso(anomaly.last_seen),
            _iso(anomaly.escalated_at), _iso(anomaly.recovered_at), anomaly.reason,
            json.dumps(anomaly.metrics or {}, ensure_ascii=False, default=str),
        ])

    jobs = db.scalars(select(SyncJob).order_by(SyncJob.started_at.desc()).limit(100)).all()
    for job in jobs:
        rows["采集日志"].append([
            job.id, job.job_type, _iso(job.started_at), _iso(job.finished_at),
            job.status, job.store_count, job.record_count, job.error_message,
        ])

    return rows


def export_daily_report(path: str | Path,
                        rows: dict[str, list[list]] | None = None) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    rows = rows or {}

    for name, headers in SHEETS.items():
        worksheet = workbook.create_sheet(name)
        worksheet.append(headers)
        for row in rows.get(name, []):
            worksheet.append(row)
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions

        for cell in worksheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1E3D5C")
            cell.alignment = Alignment(horizontal="center")

        for column in worksheet.columns:
            width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 42)
            worksheet.column_dimensions[get_column_letter(column[0].column)].width = width

        header_indexes = {cell.value: cell.column for cell in worksheet[1]}
        for header in ("订单变化率", "销售额变化率", "ACOS"):
            column = header_indexes.get(header)
            if column:
                for row in range(2, worksheet.max_row + 1):
                    worksheet.cell(row, column).number_format = "0.00%"
        roas_column = header_indexes.get("ROAS")
        if roas_column:
            for row in range(2, worksheet.max_row + 1):
                worksheet.cell(row, roas_column).number_format = '0.00"x"'
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, float) and cell.number_format == "General":
                    cell.number_format = "0.00"

    workbook.save(output_path)
    return output_path


def export_daily_reports(db, report_date: date | str, output_dir: str | Path, *,
                         per_store: bool = True,
                         store_id: str | None = None) -> list[Path]:
    target = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if store_id is not None:
        return [export_daily_report(
            directory / f"JX-Alert-{store_id}-{target.isoformat()}.xlsx",
            build_daily_report_rows(db, target, store_id=store_id),
        )]

    outputs = [export_daily_report(
        directory / f"JX-Alert-{target.isoformat()}-ALL.xlsx",
        build_daily_report_rows(db, target),
    )]
    if per_store:
        for store in db.scalars(select(Store).order_by(Store.name)).all():
            outputs.append(export_daily_report(
                directory / f"JX-Alert-{store.sid}-{target.isoformat()}.xlsx",
                build_daily_report_rows(db, target, store_id=store.sid),
            ))
    return outputs
