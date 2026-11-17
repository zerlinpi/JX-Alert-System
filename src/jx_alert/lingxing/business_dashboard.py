"""Optional official MCP business cards, with a fixed read-only tool and schema gate.

Evidence: Lingxing help -> search(business_dashboard_performance_cards), verified
2026-11-17. This interface supplies dashboard metrics; public settlement-profit
reports have different dates/denominators and must not silently replace them.
"""
import asyncio
from datetime import date, timedelta
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import delete

from jx_alert.notifications.business_daily import BUSINESS_METRICS, DIMENSION, SOURCE
from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import DailyMetric, RawSnapshot
from jx_alert.store_controls import collection_states


TOOL = "business_dashboard_performance_cards"
FIELDS = [key for key, _, _ in BUSINESS_METRICS]


class BusinessDashboardError(RuntimeError):
    pass


def _credentials(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        url, key = value["url"], value["key"]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.query or
                not isinstance(key, str) or len(key) < 16 or any(c.isspace() for c in key)):
            raise ValueError
        return url, key
    except Exception:
        raise BusinessDashboardError("Invalid private Lingxing MCP credentials") from None


def _document(result):
    if getattr(result, "isError", False):
        raise BusinessDashboardError("Official business tool returned an error")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        value = structured
    else:
        blocks = [item.text for item in getattr(result, "content", []) if item.type == "text"]
        if len(blocks) != 1 or len(blocks[0].encode()) > 256 * 1024:
            raise BusinessDashboardError("Invalid official business response")
        try:
            value = json.loads(blocks[0])
        except (ValueError, TypeError):
            raise BusinessDashboardError("Invalid official business JSON") from None
    if not isinstance(value, dict):
        raise BusinessDashboardError("Invalid official business response")
    return value


def unwrap(value):
    # MCP has a code=1 envelope around code=200 business cards. OpenAPI code=0
    # is intentionally not treated as an equivalent business-dashboard payload.
    for _ in range(3):
        if not isinstance(value, dict) or value.get("success") is False:
            raise BusinessDashboardError("Business dashboard request failed")
        if "code" in value and value["code"] not in (1, 200):
            raise BusinessDashboardError("Business dashboard request failed")
        if "current" in value or "toolId" in value:
            return value
        if "data" not in value:
            raise BusinessDashboardError("Business dashboard response is incomplete")
        value = value["data"]
    raise BusinessDashboardError("Business dashboard response nesting is invalid")


def _values(source):
    if not isinstance(source, dict):
        raise BusinessDashboardError("Invalid business metric object")
    result = {}
    for field in FIELDS:
        raw = source.get(field)
        if raw is None or raw == "":
            continue
        if isinstance(raw, bool):
            raise BusinessDashboardError("Invalid business numeric value")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise BusinessDashboardError("Invalid business numeric value") from None
        if not math.isfinite(value) or (field == "volume" and (value < 0 or not value.is_integer())):
            raise BusinessDashboardError("Invalid business numeric value")
        result[field] = value
    return result


def persist_business_cards(db, sid, report_date, response):
    """Replace exact observed partitions, keeping immutable raw response versions."""
    day = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    data = unwrap(response)
    currency = data.get("currencyCode")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise BusinessDashboardError("Business currency is unconfirmed")
    observations = [(day, _values(data.get("current")))]
    prior = day - timedelta(days=1)
    # Never infer the comparison day from field order or trust source ratios.
    if (data.get("chainCompareStartDate") == prior.isoformat()
            and data.get("chainCompareEndDate") == prior.isoformat()):
        observations.append((prior, _values(data.get("chainCompare"))))
    db.add(RawSnapshot(source=SOURCE, store_id=str(sid), payload={
        "tool": TOOL, "request": {"sid": str(sid), "report_date": day.isoformat()},
        "response": response}))
    for on, metrics in observations:
        # A complete empty response remains empty; it does not become a zero fact.
        db.execute(delete(DailyMetric).where(DailyMetric.store_id == str(sid),
            DailyMetric.dimension == DIMENSION, DailyMetric.metric_date == on))
        for name, value in metrics.items():
            db.add(DailyMetric(store_id=str(sid), metric_date=on, dimension=DIMENSION,
                entity_id=str(sid), metric_name=name, value=value,
                currency=currency, source=SOURCE))
        mark_coverage(db, store_id=str(sid), metric_date=on, dimension=DIMENSION,
            row_count=int(bool(metrics)))
    db.commit()
    return {"store_id": str(sid), "dates": [on.isoformat() for on, _ in observations],
        "metrics": sum(len(values) for _, values in observations)}


def _failed_partition(db, sid, day):
    # Retain the last good facts/raw versions, but never present them as the
    # outcome of a later failed refresh of this exact business partition.
    row = mark_coverage(db, store_id=str(sid), metric_date=date.fromisoformat(day),
                        dimension=DIMENSION, row_count=0)
    row.status = "FAILED"
    db.commit()


async def _collect(db, settings, stores, report_dates):
    url, key = _credentials(settings.lingxing_business_credentials_file)
    results, failures = [], []
    async with httpx.AsyncClient(headers={"X-Mcp-Key": key},
            timeout=httpx.Timeout(30, read=90), follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                if not {"help", "search", "action"}.issubset(tools):
                    raise BusinessDashboardError("Official MCP discovery tools are unavailable")
                discovery = _document(await session.call_tool("help", {"query": TOOL, "limit": 10}))
                listing = discovery.get("data", {}).get("tools", [])
                if not any(t.get("toolId") == TOOL and t.get("toolType") == "read" for t in listing):
                    raise BusinessDashboardError("Business tool is not confirmed read-only")
                schema = unwrap(_document(await session.call_tool("search", {"toolId": TOOL})))
                if schema.get("toolId") != TOOL or schema.get("toolType") != "read":
                    raise BusinessDashboardError("Business tool is not confirmed read-only")
                properties = schema.get("inputSchema", {}).get("properties", {})
                required = {"startDate", "endDate", "currencyCode", "sids", "aggregateType", "indexName"}
                if not required.issubset(properties):
                    raise BusinessDashboardError("Official business schema changed")
                for store in stores:
                    sid = str(store["sid"])
                    if not collection_states(settings, [sid]).get(sid, False):
                        continue
                    for day in report_dates[sid]:
                        if not collection_states(settings, [sid]).get(sid, False):
                            break
                        try:
                            response = _document(await session.call_tool("action", {"toolId": TOOL,
                                "params": {"startDate": day, "endDate": day, "currencyCode": "USD",
                                    "sids": [int(sid)], "aggregateType": "2", "indexName": FIELDS}}))
                            results.append(persist_business_cards(db, sid, day, response))
                        except Exception as exc:
                            db.rollback()
                            _failed_partition(db, sid, day)
                            failures.append({"store_id": sid, "report_date": day,
                                             "error_type": type(exc).__name__})
    return {"status": "PARTIAL_FAILED" if failures else "COMPLETE", "partitions": results,
        "failures": failures, "metrics": sum(item["metrics"] for item in results)}


def collect_business_for_day(db, settings, stores_response, report_dates):
    """Optional source: its absence/failure never erases sales or stops SP collection."""
    path = Path(getattr(settings, "lingxing_business_credentials_file", ""))
    if not path.is_file():
        return {"status": "NOT_CONFIGURED", "partitions": [], "metrics": 0}
    from jx_alert.lingxing.report_dates import normalize_store_report_dates
    dates = {}
    try:
        stores = [s for s in stores_response["data"] if int(s.get("status") or 0) == 1]
        dates = {str(s["sid"]): normalize_store_report_dates(report_dates, str(s["sid"])) for s in stores}
        return asyncio.run(_collect(db, settings, stores, dates))
    except Exception as exc:
        db.rollback()
        for sid, days in dates.items():
            if collection_states(settings, [sid]).get(sid, False):
                for day in days:
                    _failed_partition(db, sid, day)
        return {"status": "PARTIAL_FAILED", "partitions": [], "metrics": 0,
            "failures": [{"error_type": type(exc).__name__}]}
