"""Owner console data from persisted daily facts, with coverage at source grain."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

from sqlalchemy import select

from jx_alert.analysis.ad_kpis import advertising_kpis
from jx_alert.analysis.store_health import store_health_snapshot
from jx_alert.bot_service import _collection_issue_for_date, _question_report_date
from jx_alert.lingxing.ad_details import DETAIL_LABELS
from jx_alert.notifications.cards import status_label
from jx_alert.operations import scoped_stores, store_report_plan
from jx_alert.storage.models import DailyMetric, DataCoverage, Store


_DATASETS = {"store": "店铺销量", "campaign_sp": "商品推广", "campaign_sb": "品牌推广",
             "campaign_sd": "展示推广", "product_ad_sp": "商品推广广告商品",
             "product_ad_sd": "展示推广广告商品", **DETAIL_LABELS}
_ADS = ("campaign_sp", "campaign_sb", "campaign_sd")
_KPI_LABELS = {"NON_ADVERSE": "未见恶化", "ADVERSE": "恶化待复核", "INSUFFICIENT_DATA": "数据不足",
               "LOW_SAMPLE": "样本不足", "NO_BASELINE": "缺少可比基线", "ZERO_DENOMINATOR": "分母为零",
               "CURRENCY_UNCONFIRMED": "币种待确认或不一致"}


def scoped_ids(settings):
    path = Path(settings.collection_scope_file)
    raw = json.loads(path.read_text(encoding="utf-8"))
    ids = raw.get("store_ids", []) if isinstance(raw, dict) else []
    if not isinstance(ids, list) or not ids:
        raise ValueError("Console requires an explicit store scope")
    stores = scoped_stores({"data": [{"sid": str(v)} for v in ids]}, path)["data"]
    return {r["sid"] for r in stores}


def store_row(sid, store, mapping):
    status = {1: "授权正常", 2: "授权失败", 0: "未启用", 3: "已停用"}
    return {"sid": sid, "name": store.name if store else "店铺 " + sid,
            "country": store.country if store else None,
            "timezone": store.timezone if store else None, "currency": store.currency_code if store else None,
            "status": status.get(store.status, "状态待核实") if store else "尚未同步",
            "ads_authorized": bool(store and store.has_ads_setting == 1 and store.status == 1),
            "owner_count": mapping.get("owner_count", 0), "group": mapping.get("group")}


def store_overview(db, settings, scope):
    from jx_alert.admin_mapping import MappingError, store_mapping_summaries
    records = {s.sid: s for s in db.scalars(select(Store).where(Store.sid.in_(scope)))}
    try:
        mappings = store_mapping_summaries(settings, scope)
    except MappingError:
        mappings = {sid: {"owner_count": None, "group": None} for sid in scope}
    rows = [store_row(sid, records.get(sid), mappings.get(sid, {})) for sid in scope]
    rows.sort(key=lambda r: ((r["name"] or "").casefold(), r["sid"]))
    return {"stores": rows, "totals": {"stores": len(rows),
             "authorized": sum(r["ads_authorized"] for r in rows),
             "mapped": sum((r["owner_count"] or 0) > 0 for r in rows),
             "mapping_unknown": sum(r["owner_count"] is None for r in rows)}}


def _rollup(facts, coverage, metric, *, money=False):
    if coverage is None or coverage.status != "COMPLETE":
        return None, None
    metrics = [f for f in facts if f.metric_name == metric]
    if (not metrics or {r.entity_id for r in metrics} != {r.entity_id for r in facts}
            or coverage.row_count > len(metrics)
            or any(not math.isfinite(r.value) or r.value < 0 for r in metrics)):
        return None, None
    currencies = {r.currency for r in metrics}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    if money and not (isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)):
        return None, None
    return math.fsum(r.value for r in metrics), currency if money else None


def _coverage_label(record, rows):
    if record is None:
        return "尚未采集"
    if record.status == "COMPLETE" and record.row_count == 0 and not rows:
        return "接口无记录"
    return status_label(record.status)


def store_detail(db, settings, store, *, requested=None, now=None):
    clock = now or datetime.now(timezone.utc)
    zone = store_report_plan([{"sid": store.sid, "status": 1}], settings.store_timezones_file, now=clock,
        allow_unknown_timezone_history=settings.allow_unconfigured_timezone_history)[0]
    latest = date.fromisoformat(zone["report_date"]) if zone.get("report_date") else None
    if requested and (latest is None or requested > latest):
        raise ValueError("所选日期尚未结束，或店铺时区待确认。")
    target = requested or latest
    selection_warning = None
    if target:
        target, _, selection_warning = _question_report_date(db, store, target, explicit=requested is not None)
    report = {"report_date": target.isoformat() if target else None,
              "latest_completed_date": latest.isoformat() if latest else None,
              "previous_date": (target - timedelta(days=1)).isoformat() if target else None,
              "requested_date": requested.isoformat() if requested else None,
              "date_basis": "店铺当地时间" if zone.get("timezone_confirmed") else "时区待确认 · 保守历史日"}
    metrics = dict.fromkeys(["orders", "units", "sales", "spend", "ad_orders"])
    currencies = dict.fromkeys(["sales", "spend"])
    coverage, breakdown, trend, kpis = [], [], [], []
    health = store_health_snapshot(db, store, settings, now=clock)
    authorized = store.status == 1
    collection_issue = _collection_issue_for_date(db, store.sid, target) if target else None
    data_warning = ("该报表日最近一次采集失败，等待重新采集校验。" if collection_issue == "DAILY_FAILED" else
                    "经营与活动日报已提交，其他维度采集未完成。" if collection_issue == "OTHER_DIMENSIONS_FAILED" else selection_warning)
    if collection_issue == "DAILY_FAILED":
        authorized = False
    if target:
        first = target - timedelta(days=13)
        partition = {(r.metric_date, r.dimension): r for r in db.scalars(select(DataCoverage).where(
            DataCoverage.store_id == store.sid, DataCoverage.metric_date.between(first, target),
            DataCoverage.dimension.in_(_DATASETS)))}
        facts = defaultdict(list)
        for row in db.scalars(select(DailyMetric).where(DailyMetric.store_id == store.sid,
                DailyMetric.metric_date.between(first, target), DailyMetric.dimension.in_(("store", *_ADS)))):
            if row.dimension != "store" or row.entity_id == store.sid:
                facts[row.metric_date, row.dimension].append(row)

        def totals(day):
            sales = {m: _rollup(facts[day, "store"], partition.get((day, "store")), m, money=m == "sales")
                     for m in ("orders", "units", "sales")}
            ads = {}
            for dim in _ADS:
                record, rows = partition.get((day, dim)), facts[day, dim]
                ads[dim] = {m: _rollup(rows, record, m, money=m in {"spend", "sales"})
                            for m in ("spend", "orders", "units", "sales", "clicks", "impressions")}
            return sales, ads

        def ad_sum(day, ads, metric):
            included = [ads[dim][metric] for dim in _ADS
                        if _coverage_label(partition.get((day, dim)), facts[day, dim]) != "接口无记录"]
            codes = {x[1] for x in included}
            if (not included or any(x[0] is None for x in included)
                    or (metric == "spend" and len(codes) != 1)):
                return None, None
            return math.fsum(x[0] for x in included), next(iter(codes))

        for dim, label in _DATASETS.items():
            record, rows = partition.get((target, dim)), facts[target, dim]
            unavailable = "采集失败待核实" if collection_issue == "DAILY_FAILED" else "店铺授权待核实"
            coverage.append({"name": label, "status": unavailable if not authorized else _coverage_label(record, rows),
                             "rows": record.row_count if authorized and record and record.status == "COMPLETE" else None})
        if authorized:
            for offset in range(13, -1, -1):
                day = target - timedelta(days=offset)
                sales, ads = totals(day)
                spend, ad_currency = ad_sum(day, ads, "spend")
                trend.append({"date": day.isoformat(), "orders": sales["orders"][0], "sales": sales["sales"][0],
                              "spend": spend, "sales_currency": sales["sales"][1], "spend_currency": ad_currency})
                if day == target:
                    metrics.update({m: pair[0] for m, pair in sales.items()}, spend=spend,
                                   ad_orders=ad_sum(day, ads, "orders")[0])
                    currencies.update(sales=sales["sales"][1], spend=ad_currency)
                    for dim, values in ads.items():
                        breakdown.append({"type": dim[-2:].upper(), "name": _DATASETS[dim],
                            "status": _coverage_label(partition.get((day, dim)), facts[day, dim]),
                            **{m: v[0] for m, v in values.items()}, "currency": values["spend"][1],
                            "sales_currency": values["sales"][1]})
            kpis = [{"code": code, "label": item["label"], "value": item["current"],
                     "currency": item["currency"], "date": item["current_date"],
                     "format": item["format"], "status": _KPI_LABELS.get(item["status"], "待核实"),
                     "baseline": item["baseline"], "change_ratio": item["change_ratio"]}
                    for code, item in advertising_kpis(db, settings, store.sid, target).items()]
    return {**report, "metrics": metrics, "currency_codes": currencies, "coverage": coverage, "data_warning": data_warning,
            "trend": trend, "ad_breakdown": breakdown, "kpis": kpis,
            "health": {"status": health["status_label"], "score": health["ahr_score"],
                       "source_rating": health["source_status_label"], "updated_at": health["source_updated_at"],
                       "reason": health["confirmation_reason"], "indicators": health["indicators"]}}
