"""Independent official product and settlement observations, not dashboard aliases.

Contracts: /new/api-docs/Statistics/AsinListNew/ and /Finance/bdSeller/.
Financial reports use settlement dates and the default Disbursed status. They
cannot overwrite the MCP dashboard or paid-order/advertising alert baselines.
"""
from datetime import date, timedelta
import math
import re

from sqlalchemy import delete, select

from jx_alert.lingxing.client import LingxingError, LingxingCollectionPaused
from jx_alert.lingxing.report_dates import normalize_store_report_dates
from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import DailyMetric, DataCoverage, RawSnapshot
from jx_alert.store_controls import collection_states


PRODUCT = "api_business_product"
FINANCE = "api_business_finance"
SOURCE = "lingxing_api_business"
FIELDS = {
    PRODUCT: {"volume": "volume", "netAmount": "net_amount", "adSpend": "spend",
              "refundCount": "return_count", "orders": "order_items"},
    FINANCE: {"volume": "totalSalesQuantity", "platformIncome": "platformIncome",
              "grossProfit": "grossProfit", "grossRate": "grossRate", "refundsRate": "refundsRate"},
}


def _number(value, key):
    if isinstance(value, bool):
        raise LingxingError("Invalid business metric")
    result = float(value)
    if not math.isfinite(result) or (key in {"volume", "orders", "refundCount"}
            and (result < 0 or not result.is_integer())):
        raise LingxingError("Invalid business metric")
    return result


def _fetch(client, sid, day, dimension, raw=None):
    method = client.product_performance_daily if dimension == PRODUCT else client.profit_report_daily
    rows, raw, total = [], [] if raw is None else raw, None
    for _ in range(100):
        response = method(sid, day, offset=len(rows), length=1000)
        raw.append(response)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict) or data.get("all_visible") is False:
            raise LingxingError("Business report scope is incomplete")
        page, count = data.get("list" if dimension == PRODUCT else "records"), data.get("total")
        if (not isinstance(page, list) or type(count) is not int or count < 0
                or total is not None and total != count):
            raise LingxingError("Business report pagination changed")
        total = count
        for row in page:
            if not isinstance(row, dict):
                raise LingxingError("Invalid business report row")
            currency = row.get("currency_code" if dimension == PRODUCT else "currencyCode")
            if not isinstance(currency, str) or not re.fullmatch(r"USD", currency.strip()):
                raise LingxingError("Business report currency is unavailable")
            if dimension == PRODUCT:
                asins = row.get("asins")
                if ({str(v) for v in row.get("sids", [])} != {str(sid)}
                        or not isinstance(asins, list) or len(asins) != 1
                        or not isinstance(asins[0], dict) or str(asins[0].get("sid")) != str(sid)
                        or not isinstance(asins[0].get("asin"), str) or not asins[0]["asin"]):
                    raise LingxingError("Product report identity did not match")
                entity = asins[0]["asin"]
            else:
                if str(row.get("sid")) != str(sid) or row.get("postedDateLocale") != day:
                    raise LingxingError("Settlement report identity or date did not match")
                entity = str(sid)
            metrics = {key: _number(row[field], key) for key, field in FIELDS[dimension].items()
                       if row.get(field) is not None and row[field] != ""}
            rows.append({"entity": entity, "currency": currency.strip(), "metrics": metrics})
        if len(rows) > total or not page and len(rows) < total:
            raise LingxingError("Business report pagination is incomplete")
        if len(rows) == total:
            if len({row["entity"] for row in rows}) != len(rows):
                raise LingxingError("Business report contains duplicate identities")
            return rows, raw
    raise LingxingError("Business report exceeded pagination limit")


def collect_api_business_for_day(client, db, settings, stores_response, report_dates):
    """No notices or business alerts; optional source failure stays isolated."""
    client = getattr(client, "api", client)
    if client is not None and not all(hasattr(client, name) for name in ("product_performance_daily", "profit_report_daily")):
        return {"status": "UNAVAILABLE", "partitions": [], "failures": []}
    partitions, failures = [], []
    for store in stores_response["data"]:
        sid = str(store["sid"])
        if int(store.get("status") or 0) != 1:
            continue
        days = normalize_store_report_dates(report_dates, sid)
        days = sorted({day for on in days for day in (on, (date.fromisoformat(on) - timedelta(days=1)).isoformat())})
        for day in days:
            for dimension in (PRODUCT, FINANCE):
                if not collection_states(settings, [sid]).get(sid, False):
                    break
                raw = []
                try:
                    if client is None:
                        raise LingxingError("OpenAPI source is unavailable")
                    rows, raw = _fetch(client, sid, day, dimension, raw)
                    db.add(RawSnapshot(source=SOURCE, store_id=sid, payload={"dimension": dimension,
                        "report_date": day, "responses": raw, "status": "COMPLETE"}))
                    db.execute(delete(DailyMetric).where(DailyMetric.store_id == sid,
                        DailyMetric.metric_date == date.fromisoformat(day), DailyMetric.dimension == dimension))
                    for row in rows:
                        for key, value in row["metrics"].items():
                            db.add(DailyMetric(store_id=sid, metric_date=date.fromisoformat(day), dimension=dimension,
                                entity_id=row["entity"], metric_name=key, value=value, currency=row["currency"], source=SOURCE))
                    mark_coverage(db, store_id=sid, metric_date=date.fromisoformat(day), dimension=dimension,
                                  row_count=len(rows))
                    db.commit()
                    partitions.append({"store_id": sid, "report_date": day, "dimension": dimension, "rows": len(rows)})
                except LingxingCollectionPaused:
                    raise
                except Exception as exc:
                    db.rollback()
                    coverage = mark_coverage(db, store_id=sid, metric_date=date.fromisoformat(day),
                                             dimension=dimension, row_count=0)
                    coverage.status = "FAILED"
                    db.add(RawSnapshot(source=SOURCE, store_id=sid, payload={"dimension": dimension,
                        "report_date": day, "status": "FAILED", "error_type": type(exc).__name__, "responses": raw}))
                    db.commit()
                    failures.append({"store_id": sid, "report_date": day, "dimension": dimension,
                                     "error_type": type(exc).__name__})
    return {"status": "PARTIAL_FAILED" if failures else "COMPLETE", "partitions": partitions, "failures": failures}


def api_business_context(db, sid, on):
    day = date.fromisoformat(on) if isinstance(on, str) else on
    result = {"report_date": day.isoformat(), "product": {}, "settlement": {}, "availability": {}}
    for dimension, name in ((PRODUCT, "product"), (FINANCE, "settlement")):
        covered = db.scalar(select(DataCoverage).where(DataCoverage.store_id == str(sid),
            DataCoverage.metric_date == day, DataCoverage.dimension == dimension))
        result["availability"][name] = ("MISSING" if covered is None else "FAILED" if covered.status != "COMPLETE"
                                       else "EMPTY" if not covered.row_count else "FIELDS_UNAVAILABLE")
        if covered is None or covered.status != "COMPLETE" or not covered.row_count:
            continue
        rows = list(db.scalars(select(DailyMetric).where(DailyMetric.store_id == str(sid),
            DailyMetric.metric_date == day, DailyMetric.dimension == dimension, DailyMetric.source == SOURCE)))
        codes = {r.currency for r in rows}
        currency = next(iter(codes)) if len(codes) == 1 else None
        if currency is None:
            continue
        values = {}
        for key in FIELDS[dimension]:
            facts = [r for r in rows if r.metric_name == key]
            if len(facts) == covered.row_count and all(math.isfinite(r.value) for r in facts):
                if key.endswith("Rate") and len(facts) != 1:
                    continue
                values[key] = math.fsum(r.value for r in facts)
        result[name] = {"currency": currency, "metrics": values,
            "basis": "产品表现，下单日" if dimension == PRODUCT else "利润报表，结算日，已发放"}
        if values:
            result["availability"][name] = "AVAILABLE"
    return result
