import json
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.notifications.cards import _KPI_STATUS

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
        "数据完整性", "广告花费币种",
    ],
    "店铺绩效": [
        "店铺ID", "店铺", "领星店铺状态码", "广告授权", "账户健康评分",
        "账户健康状态", "绩效拉取日期", "绩效源更新时间", "绩效新鲜度",
        "观测未达标指标数", "归档待处理通知数", "通知详情",
    ],
    "SKU销量": [
        "店铺ID", "店铺", "ASIN", "MSKU", "Local SKU", "日期", "订单",
        "销量", "销售额", "前一日订单", "订单较前一日变化率",
        "前一日销售额", "销售额较前一日变化率",
        "前七日订单均值", "订单较七日均值变化率",
        "前七日销售额均值", "销售额较七日均值变化率", "数据完整性", "销售额币种",
    ],
    "广告活动": [
        "店铺ID", "店铺", "广告类型", "活动ID", "活动名称", "活动状态",
        "预算类型", "预算", "日期", "花费", "订单", "销售额", "ACOS",
        "ROAS", "点击", "曝光",
        "前一日花费", "花费较前一日变化率", "前一日广告订单", "广告订单较前一日变化率",
        "前七日花费均值", "花费较七日均值变化率", "前七日广告订单均值",
        "广告订单较七日均值变化率", "最近超预算时间", "花费币种", "销售额币种",
    ],
    "广告商品": [
        "店铺ID", "店铺", "广告类型", "广告ID", "活动ID", "ASIN", "MSKU",
        "日期", "花费", "订单", "销售额", "ACOS", "ROAS", "点击", "曝光",
        "前一日花费", "花费较前一日变化率", "前一日广告订单", "广告订单较前一日变化率",
        "前七日花费均值", "花费较七日均值变化率", "前七日广告订单均值",
        "广告订单较七日均值变化率", "花费币种", "销售额币种",
    ],
    "异常记录": [
        "等级", "店铺ID", "店铺", "对象", "规则", "状态", "首次发现",
        "最近发现", "升级时间", "恢复时间", "原因", "指标",
    ],
    "采集日志": [
        "任务ID", "任务", "开始时间", "结束时间", "状态", "店铺数",
        "记录数", "错误类型",
    ],
    "原始事实": ["事实ID", "店铺ID", "日期", "维度", "对象ID", "指标", "数值", "币种", "来源", "采集状态"],
    "采集覆盖": ["店铺ID", "日期", "维度", "状态", "源记录数", "更新时间（UTC）"],
    "广告指标": ["店铺ID", "店铺", "指标", "范围", "本期日期", "数值", "参考值", "变化率", "币种",
                 "判断", "参考口径", "公式", "比较日期", "证据指纹", "证据记录数"],
    "健康快报": ["店铺ID", "店铺", "当前判定", "判定依据", "源评级（观测）", "健康评分", "绩效数据日期",
                 "源更新时间", "采集时间（UTC）", "数据状态", "归档待处理通知数", "通知数据状态",
                 "指标", "观测值（百分数）", "平台标准（百分数）", "观测判断"],
    "归档信息": ["项目", "数值"],
}

_FACT_DIMENSIONS = {"store", "sku_msku", "sku_asin", "campaign_sp", "campaign_sb", "campaign_sd",
                    "product_ad_sp", "product_ad_sd"}
_FACT_METRICS = {"orders", "units", "sales", "spend", "clicks", "impressions"}
_MONEY = {"sales", "spend"}
_SP_FIELDS = ["版本ID", "记录ID", "店铺ID", "报表日期", "活动ID", "活动名称", "小时", "范围",
              "广告ID", "投放ID", "广告组ID", "ASIN", "MSKU", "花费", "点击", "曝光", "订单", "销售额",
              "币种", "采集时间（UTC）", "源快照ID", "行键"]
SP_SHEETS = {name: list(_SP_FIELDS) for name in ("SP日报观测", "SP小时观测", "SP广告商品明细", "SP投放明细")}
SP_SHEETS["SP对账"] = ["版本ID", "记录ID", "店铺ID", "活动ID", "报表日期", "范围", "采集状态", "对账状态",
                       "源记录数", "匹配记录数", "指标", "日报数值", "小时数值", "小时减日报", "币种", "采集时间（UTC）", "错误码"]


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _currency(values):
    values = set(values)
    return next(iter(values)) if len(values) == 1 and all(
        isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) for value in values) else None


def _total(values):
    values = list(values)
    if not values or not all(_finite(value) for value in values):
        return None
    try:
        value = math.fsum(values)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _ratio(spend, sales):
    if spend is None or sales is None or spend < 0 or sales <= 0:
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
                            store_id: str | None = None, settings=None,
                            now: datetime | None = None) -> dict[str, list[list]]:
    if settings is None:
        from jx_alert.config import get_settings
        settings = get_settings()
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Excel exports require an aware clock")
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
        DailyMetric.metric_date.between(target - timedelta(days=21), target),
        DailyMetric.dimension.in_(_FACT_DIMENSIONS),
        DailyMetric.metric_name.in_(_FACT_METRICS),
    ).order_by(DailyMetric.id)).all()
    metric_map = {
        (row.store_id, row.metric_date, row.dimension, row.entity_id, row.metric_name):
            float(row.value) if _finite(row.value) else None
        for row in metrics
    }
    coverage_rows = db.scalars(select(DataCoverage).where(
        DataCoverage.store_id.in_(selected_ids) if selected_ids else DataCoverage.store_id == "__none__",
        DataCoverage.metric_date.between(target - timedelta(days=21), target),
        DataCoverage.dimension.in_(_FACT_DIMENSIONS),
    ).order_by(DataCoverage.id)).all()
    coverage = {
        (row.store_id, row.metric_date, row.dimension) for row in coverage_rows if row.status == "COMPLETE"
    }
    partition_counts = {(row.store_id, row.metric_date, row.dimension): row.row_count for row in coverage_rows}
    currencies = {(r.store_id, r.metric_date, r.dimension, r.entity_id, r.metric_name): r.currency for r in metrics}

    def currency(sid, day, dimension, entity, metric):
        return _currency([currencies.get((sid, day, dimension, str(entity), metric))])

    def complete(sid, dimension, day):
        return (sid, day, dimension) in coverage

    def value(sid, day, dimension, entity, metric):
        key = (sid, day, dimension, str(entity), metric)
        if not complete(sid, dimension, day):
            return None
        if metric in _MONEY:
            current_currency = currency(sid, target, dimension, entity, metric)
            if not current_currency or currency(sid, day, dimension, entity, metric) != current_currency:
                return None
        return metric_map.get(key)

    def average(sid, dimension, entity, metric):
        values = [value(sid, day, dimension, entity, metric) for day in history_dates[:-1]]
        total = _total(values)
        return total / 7 if total is not None else None

    rows = {name: [] for name in SHEETS}
    status_by_partition = {(r.store_id, r.metric_date, r.dimension): r.status for r in coverage_rows}
    for fact in metrics:
        if fact.metric_date == target:
            rows["原始事实"].append([fact.id, fact.store_id, fact.metric_date.isoformat(), fact.dimension,
                fact.entity_id, fact.metric_name, fact.value if _finite(fact.value) else None,
                fact.currency, fact.source, status_by_partition.get((fact.store_id, fact.metric_date, fact.dimension), "UNKNOWN")])
    for item in coverage_rows:
        rows["采集覆盖"].append([item.store_id, item.metric_date.isoformat(), item.dimension,
            item.status, item.row_count, _iso(item.completed_at)])

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

    previous_date = target - timedelta(days=1)
    baseline_dates = history_dates[:-1]

    def ad_totals_for_day(sid, day):
        store = store_map[sid]
        if not store.has_ads_setting:
            return None, None, True
        for campaign_type in ("sp", "sb", "sd"):
            if not complete(sid, f"campaign_{campaign_type}", day):
                return None, None, False
        facts = [r for r in metrics if r.store_id == sid and r.metric_date == day
                 and r.dimension in {"campaign_sp", "campaign_sb", "campaign_sd"}]
        entities = {(r.dimension, r.entity_id) for r in facts}
        if any(partition_counts.get((sid, day, dimension), 0) >
               len({entity for dim, entity in entities if dim == dimension})
               for dimension in ("campaign_sp", "campaign_sb", "campaign_sd")):
            return None, None, False
        totals = []
        for metric in ("spend", "orders"):
            relevant = [r for r in facts if r.metric_name == metric]
            if not relevant or {(r.dimension, r.entity_id) for r in relevant} != entities:
                totals.append(None)
                continue
            if metric == "spend":
                code = _currency(r.currency for r in relevant)
                current_code = _currency(r.currency for r in metrics if r.store_id == sid and
                    r.metric_date == target and r.dimension in {"campaign_sp", "campaign_sb", "campaign_sd"}
                    and r.metric_name == "spend")
                if not code or code != current_code:
                    totals.append(None)
                    continue
            totals.append(_total(r.value for r in relevant))
        return *totals, all(item is not None for item in totals)

    for store in stores:
        sid = store.sid
        sales_complete = complete(sid, "store", target)
        store_orders = value(sid, target, "store", sid, "orders")
        store_units = value(sid, target, "store", sid, "units")
        store_sales = value(sid, target, "store", sid, "sales")

        previous_store_orders = value(sid, previous_date, "store", sid, "orders")
        previous_store_sales = value(sid, previous_date, "store", sid, "sales")
        store_previous_complete = complete(sid, "store", previous_date)
        store_baseline_complete = all(
            complete(sid, "store", day) for day in baseline_dates
        )
        average_store_orders = average(sid, "store", sid, "orders") if store_baseline_complete else None
        average_store_sales = average(sid, "store", sid, "sales") if store_baseline_complete else None

        ad_spend, ad_orders, ad_complete = ad_totals_for_day(sid, target)
        previous_ad_spend, previous_ad_orders, ad_previous_complete = ad_totals_for_day(
            sid, previous_date
        )
        ad_baseline_days = [ad_totals_for_day(sid, day) for day in baseline_dates]
        ad_baseline_complete = all(item[2] for item in ad_baseline_days)
        average_ad_spend = _total(item[0] for item in ad_baseline_days) if ad_baseline_complete and store.has_ads_setting else None
        average_ad_spend = average_ad_spend / 7 if average_ad_spend is not None else None
        average_ad_orders = _total(item[1] for item in ad_baseline_days) if ad_baseline_complete and store.has_ads_setting else None
        average_ad_orders = average_ad_orders / 7 if average_ad_orders is not None else None

        rows["店铺总览"].append([
            sid, store.name, store.country or store.region, target.isoformat(),
            store_orders, store_units, store_sales, currency(sid, target, "store", sid, "sales"),
            ad_spend if ad_complete else None,
            ad_orders if ad_complete else None,
            previous_store_orders if store_previous_complete else None,
            _change_ratio(store_orders, previous_store_orders)
            if store_previous_complete else None,
            average_store_orders,
            _change_ratio(store_orders, average_store_orders),
            previous_store_sales if store_previous_complete else None,
            _change_ratio(store_sales, previous_store_sales)
            if store_previous_complete else None,
            average_store_sales,
            _change_ratio(store_sales, average_store_sales),
            previous_ad_spend if ad_previous_complete else None,
            _change_ratio(ad_spend, previous_ad_spend)
            if ad_previous_complete else None,
            average_ad_spend,
            _change_ratio(ad_spend, average_ad_spend),
            previous_ad_orders if ad_previous_complete else None,
            _change_ratio(ad_orders, previous_ad_orders)
            if ad_previous_complete else None,
            average_ad_orders,
            _change_ratio(ad_orders, average_ad_orders),
            "完整" if sales_complete and all(item is not None for item in (store_orders, store_units, store_sales))
            and (ad_complete or not store.has_ads_setting) else "部分/缺失",
            _currency(r.currency for r in metrics if r.store_id == sid and r.metric_date == target
                      and r.dimension in {"campaign_sp", "campaign_sb", "campaign_sd"} and r.metric_name == "spend"),
        ])
        health = store_health_snapshot(db, store, settings, now=clock)
        observed_breaches = (None if any(item["status"] == "INSUFFICIENT_DATA" for item in health["indicators"])
                             else len(health["issues"]))
        rows["店铺绩效"].append([
            sid, store.name, store.status,
            "已配置" if store.has_ads_setting else "未配置",
            health["ahr_score"], health["source_status_label"], health["pull_date"], health["source_updated_at"],
            health["freshness_label"], observed_breaches, health["pending_notice_observations"],
            "仅在受控系统中查看" if health["pending_notice_observations"] else None,
        ])
        for indicator in health["indicators"]:
            rows["健康快报"].append([sid, store.name, health["status_label"], health["confirmation_reason"],
                health["source_status_label"], health["ahr_score"], health["pull_date"], health["source_updated_at"],
                health["captured_at"], health["freshness_label"], health["pending_notice_observations"],
                health["notice_freshness_label"], indicator["label"], indicator["value"], indicator["standard"],
                indicator["status_label"]])
        comparisons = advertising_kpis(db, settings, sid, target)
        for code, entry in comparisons.items():
            rows["广告指标"].append([sid, store.name, code, "店铺全部广告", entry["current_date"],
                entry["current"], entry["baseline"], entry["change_ratio"], entry["currency"],
                _KPI_STATUS.get(entry["status"], "待确认"),
                {"SEVEN_DAY": "前七日汇总比率", "PREVIOUS_DAY": "前一日"}.get(entry["comparison_basis"], "数据不足"),
                entry["formula"], "、".join(entry["comparison_dates"]), entry["fingerprint"], entry["evidence_count"]])

        store_products = [product for product in products if product.store_id == sid]
        msku_current_complete = complete(sid, "sku_msku", target)
        msku_previous_complete = complete(sid, "sku_msku", previous_date)
        msku_baseline_complete = all(
            complete(sid, "sku_msku", day) for day in baseline_dates
        )
        for product in store_products:
            current_orders = value(sid, target, "sku_msku", product.seller_sku, "orders")
            current_units = value(sid, target, "sku_msku", product.seller_sku, "units")
            current_sales = value(sid, target, "sku_msku", product.seller_sku, "sales")
            previous_orders = value(
                sid, previous_date, "sku_msku", product.seller_sku, "orders"
            )
            previous_sales = value(
                sid, previous_date, "sku_msku", product.seller_sku, "sales"
            )
            if msku_baseline_complete:
                average_orders = average(sid, "sku_msku", product.seller_sku, "orders")
                average_sales = average(sid, "sku_msku", product.seller_sku, "sales")
            else:
                average_orders = None
                average_sales = None
            rows["SKU销量"].append([
                sid, store.name, product.asin, product.seller_sku, product.local_sku,
                target.isoformat(), current_orders, current_units, current_sales,
                previous_orders if msku_previous_complete else None,
                _change_ratio(current_orders, previous_orders)
                if msku_previous_complete else None,
                previous_sales if msku_previous_complete else None,
                _change_ratio(current_sales, previous_sales)
                if msku_previous_complete else None,
                average_orders, _change_ratio(current_orders, average_orders),
                average_sales, _change_ratio(current_sales, average_sales),
                "完整" if msku_current_complete and all(item is not None for item in
                    (current_orders, current_units, current_sales)) else "样本不足",
                currency(sid, target, "sku_msku", product.seller_sku, "sales"),
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
        previous_complete = complete(campaign.store_id, dimension, previous_date)
        baseline_complete = all(
            complete(campaign.store_id, dimension, day) for day in baseline_dates
        )
        previous_spend = value(
            campaign.store_id, previous_date, dimension, campaign.campaign_id, "spend"
        )
        previous_orders = value(
            campaign.store_id, previous_date, dimension, campaign.campaign_id, "orders"
        )
        average_spend = average(campaign.store_id, dimension, campaign.campaign_id, "spend") if baseline_complete else None
        average_orders = average(campaign.store_id, dimension, campaign.campaign_id, "orders") if baseline_complete else None
        spend_currency = currency(campaign.store_id, target, dimension, campaign.campaign_id, "spend")
        sales_currency = currency(campaign.store_id, target, dimension, campaign.campaign_id, "sales")
        matched_currency = spend_currency and spend_currency == sales_currency
        rows["广告活动"].append([
            campaign.store_id, store.name, campaign.campaign_type.upper(),
            campaign.campaign_id, campaign.name, campaign.state, campaign.budget_type,
            campaign.budget, target.isoformat(), spend, orders, sales,
            _ratio(spend, sales) if matched_currency else None,
            _roas(spend, sales) if matched_currency else None, clicks, impressions,
            previous_spend if previous_complete else None,
            _change_ratio(spend, previous_spend) if previous_complete else None,
            previous_orders if previous_complete else None,
            _change_ratio(orders, previous_orders) if previous_complete else None,
            average_spend, _change_ratio(spend, average_spend),
            average_orders, _change_ratio(orders, average_orders),
            campaign.last_over_budget_at, spend_currency, sales_currency,
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
        previous_complete = complete(ad.store_id, dimension, previous_date)
        baseline_complete = all(
            complete(ad.store_id, dimension, day) for day in baseline_dates
        )
        previous_spend = value(ad.store_id, previous_date, dimension, ad.ad_id, "spend")
        previous_orders = value(ad.store_id, previous_date, dimension, ad.ad_id, "orders")
        average_spend = average(ad.store_id, dimension, ad.ad_id, "spend") if baseline_complete else None
        average_orders = average(ad.store_id, dimension, ad.ad_id, "orders") if baseline_complete else None
        spend_currency = currency(ad.store_id, target, dimension, ad.ad_id, "spend")
        sales_currency = currency(ad.store_id, target, dimension, ad.ad_id, "sales")
        matched_currency = spend_currency and spend_currency == sales_currency
        rows["广告商品"].append([
            ad.store_id, store.name, ad.campaign_type.upper(), ad.ad_id,
            ad.campaign_id, ad.asin, ad.seller_sku, target.isoformat(),
            spend, orders, sales, _ratio(spend, sales) if matched_currency else None,
            _roas(spend, sales) if matched_currency else None,
            clicks, impressions,
            previous_spend if previous_complete else None,
            _change_ratio(spend, previous_spend) if previous_complete else None,
            previous_orders if previous_complete else None,
            _change_ratio(orders, previous_orders) if previous_complete else None,
            average_spend, _change_ratio(spend, average_spend),
            average_orders, _change_ratio(orders, average_orders),
            spend_currency, sales_currency,
        ])

    for anomaly in anomalies:
        store = store_map.get(anomaly.store_id)
        rows["异常记录"].append([
            anomaly.severity, anomaly.store_id, store.name if store else "",
            anomaly.entity_id, anomaly.rule, anomaly.status,
            _iso(anomaly.first_seen), _iso(anomaly.last_seen),
            _iso(anomaly.escalated_at), _iso(anomaly.recovered_at), anomaly.reason,
            json.dumps({key: val for key, val in (anomaly.metrics or {}).items()
                if key in _FACT_METRICS | {"current", "previous", "baseline_7d", "change_ratio"}
                and (val is None or _finite(val))}, ensure_ascii=False),
        ])

    jobs = db.scalars(select(SyncJob).order_by(SyncJob.started_at.desc()).limit(100)).all()
    for job in jobs:
        rows["采集日志"].append([
            job.id, job.job_type, _iso(job.started_at), _iso(job.finished_at),
            job.status, job.store_count, job.record_count,
            (job.metadata_json or {}).get("error_type")
            if isinstance((job.metadata_json or {}).get("error_type"), str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", (job.metadata_json or {}).get("error_type")) else None,
        ])

    rows["归档信息"] = [["报表日期", target.isoformat()], ["导出时间（UTC）", clock.astimezone(timezone.utc).isoformat()],
        ["原始事实范围", "指定报表日；标准化运营事实，不含客户信息或接口凭据"],
        ["覆盖状态窗口", f"{(target - timedelta(days=21)).isoformat()} 至 {target.isoformat()}"],
        ["缺失值规则", "空单元格表示缺失或不可比，真实零值保留0"],
        ["广告指标日期", "流量与费用使用报表日；转化指标使用成熟归因日，详见本期日期"],
        ["绩效规则", "仅源观测；数据质量未确认不得认定当前健康"]]

    return rows


def export_daily_report(path: str | Path,
                        rows: dict[str, list[list]] | None = None, *, sheet_names=None) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    rows = rows or {}

    schemas = {**SHEETS, **SP_SHEETS}
    for name in (list(SHEETS) if sheet_names is None else sheet_names):
        headers = schemas[name]
        worksheet = workbook.create_sheet(name)
        worksheet.append(headers)
        for row in rows.get(name, []):
            if len(row) > len(headers):
                raise ValueError("Excel row exceeds its declared schema")
            if worksheet.max_row >= 1_048_576:
                raise ValueError("Excel worksheet row limit exceeded; export a smaller partition")
            prepared = []
            for index, item in enumerate(row):
                if item is not None and headers[index].endswith("ID"):
                    item = str(item)
                if isinstance(item, str):
                    item = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", item)
                    if len(item) > 32767:
                        raise ValueError("Excel cell exceeds its text limit")
                if isinstance(item, float) and not math.isfinite(item):
                    item = None
                prepared.append(item)
            worksheet.append(prepared)
            for cell in worksheet[worksheet.max_row]:
                # All external text, including =, +, -, and @ prefixes, remains
                # literal text. No untrusted workbook formulas are authored.
                if isinstance(cell.value, str):
                    cell.data_type = "s"
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
        for header, column in header_indexes.items():
            if "变化率" in str(header) or header == "ACOS":
                for row in range(2, worksheet.max_row + 1):
                    worksheet.cell(row, column).number_format = "0.00%"
        roas_column = header_indexes.get("ROAS")
        if roas_column:
            for row in range(2, worksheet.max_row + 1):
                worksheet.cell(row, roas_column).number_format = '0.00"x"'
        if name == "广告指标":
            for row in range(2, worksheet.max_row + 1):
                code = worksheet.cell(row, 3).value
                display = "0.00%" if code in {"CTR", "CDR", "TCOAS"} else '0.00"倍"' if code == "ROAS" else "0.00"
                for column in (6, 7):
                    worksheet.cell(row, column).number_format = display
        if name == "健康快报":
            for row in range(2, worksheet.max_row + 1):
                for column in (14, 15):
                    worksheet.cell(row, column).number_format = '0.00"%"'
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, float) and cell.number_format == "General":
                    cell.number_format = "0.00"

    handle, temporary = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    os.close(handle)
    try:
        workbook.save(temporary)
        os.chmod(temporary, 0o600)
        with open(temporary, "rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    finally:
        workbook.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output_path


def export_daily_reports(db, report_date: date | str, output_dir: str | Path, *,
                         per_store: bool = True,
                         store_id: str | None = None, settings=None) -> list[Path]:
    target = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if store_id is not None:
        store_id = _store_id(store_id)
        return [export_daily_report(
            directory / f"JX-Alert-{store_id}-{target.isoformat()}.xlsx",
            build_daily_report_rows(db, target, store_id=store_id, settings=settings),
        )]

    outputs = [export_daily_report(
        directory / f"JX-Alert-{target.isoformat()}-ALL.xlsx",
        build_daily_report_rows(db, target, settings=settings),
    )]
    if per_store:
        for store in db.scalars(select(Store).order_by(Store.name)).all():
            sid = _store_id(store.sid)
            outputs.append(export_daily_report(
                directory / f"JX-Alert-{sid}-{target.isoformat()}.xlsx",
                build_daily_report_rows(db, target, store_id=sid, settings=settings),
            ))
    return outputs


def _store_id(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("Excel archive store_id must be a positive numeric identifier")
    return str(value)


def _within(root, candidate):
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Excel archive target is outside its configured directory")
    return resolved


def _atomic_copy(source, target):
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as output, source.open("rb") as input_file:
            while data := input_file.read(1024 * 1024):
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_collected_store(db, store_id, report_date, output_dir, *, settings=None, now=None):
    """Atomically archive one collected store/day, with immutable content versions.

    Call after each committed collection step, including partial collection.
    The returned paths are ``[latest, version]``. Repeated identical evidence
    reuses its version; neither file ever appends spend or creates synthetic rows.
    """
    sid = _store_id(store_id)
    target = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    if type(target) is not date:
        raise ValueError("Excel archive report_date must be a date")
    root = Path(output_dir).resolve()
    directory = _within(root, root / f"{target:%Y}" / f"{target:%m}" / f"{target:%d}")
    rows = build_daily_report_rows(db, target, store_id=sid, settings=settings, now=now)
    canonical = {key: data for key, data in rows.items() if key not in {"归档信息", "采集日志"}}
    version = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    rows["归档信息"].extend([["数据版本（SHA256）", version], ["店铺ID", sid],
                               ["版本规则", "相同内容复用版本；最新文件原子替换，历史版本保留"]])
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = _within(root, directory / f"JX-Alert-{sid}-{target}-{version}.xlsx")
    latest = _within(root, directory / f"JX-Alert-{sid}-{target}-最新.xlsx")
    if not snapshot.exists():
        export_daily_report(snapshot, rows)
    _atomic_copy(snapshot, latest)
    return [latest, snapshot]


def archive_sp_run(db, run_id, output_dir):
    """Export one persisted SP run without mixing observations from other versions."""
    from jx_alert.storage.sp import (
        CollectionLog, ReconciliationLog, SPAdvertiseDetail, SPDailyReport,
        SPHourlyReport, SPTargetDetail,
    )
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ValueError("SP Excel archive requires a valid run ID")
    run = db.get(CollectionLog, run_id)
    if run is None:
        raise ValueError("SP Excel archive requires a persisted collection run")
    sid = _store_id(run.store_id)
    day = run.finished_at.date()
    root = Path(output_dir).resolve()
    directory = _within(root, root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "SP")
    path = _within(root, directory / f"SP-{sid}-{run_id}.xlsx")
    if path.exists():
        return [path]
    rows = {name: [] for name in SP_SHEETS}
    observations = []
    for name, model in [("SP日报观测", SPDailyReport), ("SP小时观测", SPHourlyReport),
                        ("SP广告商品明细", SPAdvertiseDetail), ("SP投放明细", SPTargetDetail)]:
        selected = db.scalars(select(model).where(model.run_id == run_id).order_by(model.id)).all()
        observations.extend((name, item) for item in selected)
    dates = {item.report_date for _, item in observations}
    money_rows = db.scalars(select(DailyMetric).where(DailyMetric.store_id == sid,
        DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend",
        DailyMetric.metric_date.in_(dates))).all() if dates else []
    codes = {(item.metric_date, item.entity_id): _currency([item.currency]) for item in money_rows}
    for name, item in observations:
        rows[name].append([run_id, item.id, item.store_id, item.report_date.isoformat(), item.campaign_id,
            getattr(item, "campaign_name", None), getattr(item, "hour", None), getattr(item, "scope", None),
            getattr(item, "ad_id", None), getattr(item, "targeting_id", None), getattr(item, "group_id", None),
            getattr(item, "asin", None), getattr(item, "seller_sku", None), item.cost, item.clicks,
            item.impressions, item.orders, item.sales, codes.get((item.report_date, item.campaign_id)),
            _iso(item.captured_at), item.source_snapshot_id, item.row_key])
    reconciliations = db.scalars(select(ReconciliationLog).where(
        ReconciliationLog.run_id == run_id).order_by(ReconciliationLog.id)).all()
    for item in reconciliations:
        for metric in ("cost", "clicks", "impressions", "orders", "sales"):
            def observed(values):
                value = (values or {}).get(metric)
                if isinstance(value, bool) or value is None:
                    return None
                try:
                    numeric = float(value)
                except (TypeError, ValueError, OverflowError):
                    return None
                return numeric if math.isfinite(numeric) else None
            rows["SP对账"].append([run_id, item.id, item.store_id, item.campaign_id, item.report_date.isoformat(),
                item.scope, item.collection_status, item.comparison_status, item.row_count, item.matched_row_count,
                metric, observed(item.daily_metrics), observed(item.hourly_metrics), observed(item.delta_hour_minus_daily),
                codes.get((item.report_date, item.campaign_id)) if metric in {"cost", "sales"} else None,
                _iso(item.captured_at),
                item.error_code if item.error_code and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item.error_code) else None])
    rows["归档信息"] = [["采集版本ID", run_id], ["店铺ID", sid], ["活动ID", run.campaign_id],
        ["采集状态", run.status], ["采集开始（UTC）", _iso(run.started_at)], ["采集完成（UTC）", _iso(run.finished_at)],
        ["源内容SHA256", run.content_sha256], ["版本规则", "仅本次run_id的独立观测，不与历史版本合加"],
        ["对账规则", "日报与小时数据保留原始差异，不人为补齐"], ["币种规则", "仅已确认同店/日/活动的广告币种；未确认留空"],
        ["字段范围", "仅标准化运营白名单字段，不导出源响应或客户信息"]]
    return [export_daily_report(path, rows, sheet_names=[*SP_SHEETS, "归档信息"])]
