"""Fixed official MCP read contracts verified through help/search on 2026-11-17.

Purchase-date product performance is kept separate from paid-order OpenAPI
sales. Advertising UI attribution is unspecified and is never copied to mature
conversion facts. The caller owns persistence, store controls and selection.
"""
import asyncio
from contextlib import AsyncExitStack
import json
import math
import re

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from jx_alert.lingxing.business_dashboard import BusinessDashboardError, _visible_store_ids
from jx_alert.lingxing.source_credentials import mcp_credentials, SourceCredentialsError
from jx_alert.lingxing.mcp_pacing import paced_call
from jx_alert.store_controls import collection_states


TOOLS = frozenset({"get_my_sids", "ad_auth_shops", "query_product_performance_asin_lists",
    "ad_campaign_report", "customer_store_performance_list", "customer_store_target_detail",
    "customer_performance_notice_list"})


class MCPReadError(BusinessDashboardError):
    pass


def document(result):
    if getattr(result, "isError", False):
        raise MCPReadError("Official MCP read failed")
    value = getattr(result, "structuredContent", None)
    if value is None:
        parts = [x.text for x in getattr(result, "content", []) if x.type == "text"]
        if len(parts) != 1 or len(parts[0].encode()) > 2 * 1024 * 1024:
            raise MCPReadError("Official MCP response is not bounded JSON")
        value = json.loads(parts[0])
    if not isinstance(value, dict):
        raise MCPReadError("Invalid official MCP object")
    return value


def business_body(value):
    # The actual generic action response has exactly one outer code=1 envelope.
    if (not isinstance(value, dict) or value.get("success") is False or value.get("code") != 1
            or not isinstance(value.get("data"), dict)):
        raise MCPReadError("Official MCP envelope failed")
    body = value["data"]
    if body.get("code") != 1 or body.get("success") is False:
        raise MCPReadError("Official business query failed")
    return body


def numeric(value, *, count=False):
    if isinstance(value, bool) or value is None:
        raise MCPReadError("Missing observed numeric value")
    try:
        result = float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        raise MCPReadError("Invalid observed numeric value") from None
    if not math.isfinite(result) or result < 0 or (count and not result.is_integer()):
        raise MCPReadError("Invalid observed numeric value")
    return result


class OfficialReadSource:
    def __init__(self, settings):
        self.settings, self.schemas, self.cache = settings, {}, {}
        # Match the primary client's configuration snapshot: one collection
        # cannot mix an old visibility cache with a newly rotated identity.
        try:
            self._credentials = mcp_credentials(settings)
        except SourceCredentialsError:
            self._credentials = None

    async def _invoke(self, tool, params):
        if tool not in TOOLS:
            raise MCPReadError("Tool is not in the fixed read allowlist")
        credentials = self._credentials
        if credentials is None:
            raise MCPReadError("Official MCP is not configured")
        url, key = credentials
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(headers={"X-Mcp-Key": key},
                timeout=httpx.Timeout(30, read=90), follow_redirects=False, trust_env=False))
            read, write, _ = await stack.enter_async_context(streamable_http_client(url, http_client=http))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            if tool not in self.schemas:
                found = document(await paced_call(session, "help", {"query": tool, "limit": 10}))
                if not any(t.get("toolId") == tool and t.get("toolType") == "read"
                           for t in found.get("data", {}).get("tools", []) if isinstance(t, dict)):
                    raise MCPReadError("Official tool is not confirmed read-only")
                schema = document(await paced_call(session, "search", {"toolId": tool})).get("data", {})
                if schema.get("toolId") != tool or schema.get("toolType") != "read":
                    raise MCPReadError("Official read schema is unconfirmed")
                self.schemas[tool] = schema.get("inputSchema", {})
            schema = self.schemas[tool]
            if (schema.get("type") != "object" or not set(params) <= set(schema.get("properties", {}))
                    or not set(schema.get("required", [])) <= set(params)):
                raise MCPReadError("Official read parameters changed")
            return document(await paced_call(session, "action", {"toolId": tool, "params": params}))

    def call(self, tool, params):
        key = (tool, json.dumps(params, sort_keys=True, separators=(",", ":")))
        if key not in self.cache:
            self.cache[key] = asyncio.run(self._invoke(tool, params))
        return self.cache[key]

    def stores(self):
        response = self.call("get_my_sids", {})
        _visible_store_ids(response)
        return business_body(response)["list"], response

    def check_store(self, sid):
        if not collection_states(self.settings, [str(sid)])[str(sid)]:
            from jx_alert.lingxing.client import LingxingCollectionPaused
            raise LingxingCollectionPaused("Store collection was paused")
        if str(sid) not in {str(s["id"]) for s in self.stores()[0]}:
            raise MCPReadError("Store is not visible in the official MCP")

    def profiles(self):
        response = self.call("ad_auth_shops", {})
        rows = business_body(response).get("data")
        if not isinstance(rows, list):
            raise MCPReadError("Invalid official advertising profile inventory")
        return rows, response

    def sales(self, sid, day, dimension):
        self.check_store(sid)
        if dimension not in {"asin", "msku"}:
            raise MCPReadError("Unsupported product grain")
        rows, pages, expected = [], [], None
        for offset in range(0, 4000, 20):
            self.check_store(sid)
            params = {"start_date": day, "end_date": day, "sids": str(sid), "date_type": "purchase",
                "date_view_type": "day", "date_view_order_type": 1, "summary_field": dimension,
                "currency_code": "USD", "offset": offset, "length": 20, "is_recently_enum": False,
                "sort_field": "volume", "sort_type": "desc"}
            response = self.call("query_product_performance_asin_lists", params)
            data = business_body(response).get("data")
            if not isinstance(data, dict) or data.get("all_visible") is not True:
                raise MCPReadError("Product report visibility is incomplete")
            page, total = data.get("list"), data.get("total")
            if (not isinstance(page, list) or type(total) is not int or total < 0
                    or expected is not None and total != expected):
                raise MCPReadError("Product pagination is inconsistent")
            expected = total
            pages.append({"request": params, "response": response})
            for row in page:
                if (not isinstance(row, dict) or row.get("sids") != [int(sid)] or row.get("rdate") != day
                        or row.get("row_type") != dimension or not re.fullmatch(r"[A-Z]{3}", row.get("currency_code") or "")):
                    raise MCPReadError("Product scope, date or currency did not match")
                entity = row.get("asin" if dimension == "asin" else "msku")
                if not isinstance(entity, str) or not entity.strip():
                    raise MCPReadError("Product identity is missing")
                rows.append({"entity_id": entity, "sales": numeric(row.get("amount")),
                    "units": numeric(row.get("volume"), count=True),
                    "orders": numeric(row.get("order_items"), count=True), "currency": row["currency_code"]})
            if len(rows) > total or not page and len(rows) < total:
                raise MCPReadError("Product pagination is incomplete")
            if len(rows) == total:
                if len({r["entity_id"] for r in rows}) != len(rows):
                    raise MCPReadError("Product report contains duplicate identities")
                return rows, pages
        raise MCPReadError("Product report exceeded page limit")

    def campaign_ads(self, sid, day, kind):
        self.check_store(sid)
        profiles = [r for r in self.profiles()[0] if str(r.get("sid")) == str(sid)
                    and r.get("type") == "seller" and r.get("real_status") == 1]
        profile_ids = [str(r["profile_id"]) for r in profiles if str(r.get("profile_id", "")).isdigit()]
        if not profile_ids or kind not in {"sp", "sb", "sd"}:
            raise MCPReadError("An exact active advertising profile is unavailable")
        rows, pages, expected = [], [], None
        for page_number in range(1, 101):
            self.check_store(sid)
            params = {"report_date": f"{day} - {day}", "profile_ids": profile_ids,
                "ads_type": [[kind]], "page": page_number, "length": 1000,
                "sort_field": "spends", "sort_type": "desc"}
            response = self.call("ad_campaign_report", params)
            body = business_body(response)
            page, total = body.get("data"), body.get("recordsFiltered")
            if (not isinstance(page, list) or type(total) is not int or total < 0
                    or expected is not None and total != expected):
                raise MCPReadError("Advertising pagination is inconsistent")
            expected = total
            pages.append({"request": params, "response": response})
            # The first row is a source aggregate with null campaign/profile.
            detail = [r for r in page if isinstance(r, dict) and r.get("campaign_id") is not None]
            if len(page) - len(detail) > 1 or any(not isinstance(r, dict) for r in page):
                raise MCPReadError("Invalid advertising report rows")
            for row in detail:
                if str(row.get("profile_id")) not in profile_ids or row.get("ads_type") != kind:
                    raise MCPReadError("Advertising profile or type does not match")
                rows.append({"entity_id": str(row["campaign_id"]), "profile_id": str(row["profile_id"]),
                    "name": str(row.get("name") or row["campaign_id"]), "spend": numeric(row.get("spends")),
                    "clicks": numeric(row.get("clicks"), count=True),
                    "impressions": numeric(row.get("impressions"), count=True)})
            if len(rows) > total or not detail and len(rows) < total:
                raise MCPReadError("Advertising pagination is incomplete")
            if len(rows) == total:
                if len({(r["profile_id"], r["entity_id"]) for r in rows}) != len(rows):
                    raise MCPReadError("Advertising report contains duplicate identities")
                return rows, pages
        raise MCPReadError("Advertising report exceeded page limit")
