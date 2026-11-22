"""Read-only SparkXAI MCP transport. Never translate vendor report content."""

from datetime import date, timedelta
import json

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

SPARKX_MCP_URL = "https://mcp.sparkx.cn/mcp"
_READ_TOOLS = frozenset({"get_ads_perf", "get_user_authorized_context"})
_ENTITIES = frozenset({
    "campaign", "adGroup", "target", "searchTerm", "placement", "productAd", "asin",
    "keywordPlacement", "campaignAudience",
})
_QUERY_KEYS = frozenset({
    "profileIds", "factEntity", "dateStart", "dateEnd", "metrics", "userContext", "select",
    "groupBy", "filters", "orderBy", "page", "pageSize", "language", "queryType", "timeGranularity", "search",
})


class SparkXDirectError(ValueError):
    """Safe to show to a caller; must not contain remote bodies or credentials."""


def parse_profile_ids(raw: str) -> frozenset[int]:
    parts = [part.strip() for part in raw.split(",")]
    if any(not part.isascii() or not part.isdecimal() or not 1 <= len(part) <= 20
           for part in parts):
        raise SparkXDirectError("SPARKX_PROFILE_IDS must be a comma-separated list of profile IDs")
    ids = frozenset(int(part) for part in parts)
    if 0 in ids or max(ids) > 2**63 - 1:
        raise SparkXDirectError("SPARKX_PROFILE_IDS contains an invalid profile ID")
    return ids


def validate_token(token: str) -> None:
    if not token or len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
        raise SparkXDirectError("A valid SPARKX_AI_TOKEN is required")


def _day(value) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (ValueError, TypeError):
        pass
    raise SparkXDirectError("Dates must use YYYY-MM-DD")


def page_size_for(query):
    return query.get("pageSize", 50 if query["factEntity"] == "campaignAudience" else 100)


def validate_performance_query(arguments: dict, allowed_profile_ids: frozenset[int]) -> None:
    profiles = arguments.get("profileIds")
    if (not isinstance(profiles, list) or len(profiles) != 1 or type(profiles[0]) is not int
            or profiles[0] <= 0 or profiles[0] not in allowed_profile_ids):
        raise SparkXDirectError("Request exactly one explicitly allowed SparkX profile ID")
    if "tenantId" in arguments or "userId" in arguments:
        raise SparkXDirectError("SparkX tenant and user are determined by the service token")
    if arguments.keys() - _QUERY_KEYS:
        raise SparkXDirectError("Unknown SparkX performance argument")
    fact = arguments.get("factEntity")
    if not isinstance(fact, str) or fact not in _ENTITIES:
        raise SparkXDirectError("Choose a documented SparkX factEntity")
    start, end = _day(arguments.get("dateStart")), _day(arguments.get("dateEnd"))
    audience = fact == "campaignAudience"
    if audience:
        if arguments.keys() & {"groupBy", "queryType"}:
            raise SparkXDirectError("Omit groupBy and queryType for campaignAudience; its row scope is fixed")
        if "orderBy" in arguments and (not isinstance(arguments["orderBy"], list) or len(arguments["orderBy"]) > 1):
            raise SparkXDirectError("SparkX campaignAudience accepts at most one orderBy rule")
    if "search" in arguments:
        search = arguments["search"]
        if not audience or not isinstance(search, str) or not search.isprintable() or not search.strip() or len(search) > 200:
            raise SparkXDirectError("SparkX search requires campaignAudience and 1–200 printable characters")
    if "metrics" in arguments or not audience:
        metrics = arguments.get("metrics")
        if (not isinstance(metrics, list) or not metrics
                or any(not isinstance(metric, str) or not metric.strip() for metric in metrics)):
            raise SparkXDirectError("Provide non-empty SparkX metric names")
    context = arguments.get("userContext")
    if not isinstance(context, str) or not context.strip() or len(context) > 100:
        raise SparkXDirectError("userContext must be a short non-empty string of at most 100 characters")
    fields = arguments.get("select", [])
    if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
        raise SparkXDirectError("select must be a list of SparkX field names")
    granularity = arguments.get("timeGranularity", "daily")
    if not isinstance(granularity, str) or granularity.lower() not in {"", "daily", "hourly"}:
        raise SparkXDirectError("timeGranularity must be daily or hourly")
    hourly = granularity.lower() == "hourly"
    if hourly and fact != "campaign":
        raise SparkXDirectError("Hourly granularity is supported only for campaign")
    if fact == "keywordPlacement" and "timeGranularity" in arguments:
        raise SparkXDirectError("Omit timeGranularity for keywordPlacement")
    if (end - start).days not in range(7 if hourly or fact == "keywordPlacement" else 90):
        raise SparkXDirectError("Limit queries to 90 daily dates or 7 hourly dates")
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise SparkXDirectError("SparkX query requires valid JSON") from None
    if len(encoded) > 32_768:
        raise SparkXDirectError("SparkX query exceeds the request size limit")
    for key, maximum in (("page", None), ("pageSize", 200 if audience else 500)):
        if key in arguments:
            val = arguments[key]
            if type(val) is not int or val < 1 or (maximum is not None and val > maximum):
                raise SparkXDirectError(f"Invalid SparkX {key}")


async def call_sparkx(tool_name: str, arguments: dict, *, token: str,
                      timeout_seconds: float = 30, transport=None) -> CallToolResult:
    """Use the fixed vendor origin and never relay write tools or follow redirects."""
    if tool_name not in _READ_TOOLS:
        raise SparkXDirectError("SparkX tool is not on the read-only allowlist")
    if tool_name == "get_user_authorized_context" and arguments:
        raise SparkXDirectError("SparkX context lookup does not accept arguments")
    validate_token(token)
    try:
        with anyio.fail_after(timeout_seconds):
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token},
                timeout=httpx.Timeout(timeout_seconds),
                follow_redirects=False, trust_env=False, transport=transport,
            ) as http:
                async with streamable_http_client(SPARKX_MCP_URL, http_client=http) as (
                    read, write, _,
                ):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    ) as session:
                        await session.initialize()
                        return await session.call_tool(tool_name, arguments)
    except Exception:
        # Streamable HTTP wraps failures in task-group exception chains; never expose
        # the upstream response body, headers, token or raw exception to a caller.
        raise SparkXDirectError(
            "SparkX MCP request failed; check authorization, connectivity and timeout"
        ) from None


def check_authorized_profiles(result: CallToolResult, profile_ids: frozenset[int]) -> None:
    """Verify only the expected shop subset without returning the tenant's directory."""
    def unique(pairs):
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError
        return data

    def roster(data):
        if (not isinstance(data, dict) or data.get("isError", False) is not False or "errorType" in data
                or data.get("toolName", "get_user_authorized_context") != "get_user_authorized_context"
                or not isinstance(data.get("profiles"), list)):
            raise ValueError
        ids = set()
        for row in data["profiles"]:
            value = row.get("profileId") if isinstance(row, dict) else None
            if type(value) is str and value.isascii() and value.isdecimal() and len(value) <= 20:
                value = int(value)
            if type(value) is not int or not 0 < value <= 2**63 - 1:
                raise ValueError
            ids.add(value)
        return ids

    if result.isError:
        raise SparkXDirectError("SparkX did not confirm upstream authorization")
    try:
        views = [roster(result.structuredContent)] if result.structuredContent is not None else []
        for block in result.content:
            if block.type != "text":
                continue
            raw = block.text.strip()
            if raw.startswith("```json"):
                if not raw.endswith("```"):
                    raise ValueError
                raw = raw[len("```json"):-3].strip()
                if not raw.startswith("{"):
                    raise ValueError
            if raw.startswith(("{", "[")):
                views.append(roster(json.loads(raw, object_pairs_hook=unique)))
        if not views or any(ids != views[0] for ids in views[1:]):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise SparkXDirectError("SparkX did not confirm consistent upstream authorization") from None
    if not profile_ids or not profile_ids.issubset(views[0]):
        raise SparkXDirectError("One or more configured SparkX profiles are not authorized")


async def _check() -> None:
    from jx_alert.config import get_settings

    settings = get_settings()
    profiles = parse_profile_ids(settings.sparkx_profile_ids)
    token = settings.sparkx_ai_token.get_secret_value()
    result = await call_sparkx("get_user_authorized_context", {}, token=token)
    check_authorized_profiles(result, profiles)
    print(f"SparkX authorization verified for {len(profiles)} configured profile(s).")


if __name__ == "__main__":
    try:
        anyio.run(_check)
    except SparkXDirectError as exc:
        raise SystemExit(str(exc)) from None
