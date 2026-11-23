"""Read-only SparkX AI MCP client (Streamable HTTP, Bearer MCP token).

Contract: https://github.com/SparkXAI/SparkXAI-MCP (skills/sparkx-query-*).
Only the read tools below can be called; write tools (batch_update_ads,
create_*, save_*, delete_*) are unreachable by construction. Failures carry a
bounded code; provider bodies, headers and the token never leave this module.
"""
from contextlib import asynccontextmanager
from datetime import date, timedelta
import json
import logging
import math
import re

import anyio
import httpx

_LOG = logging.getLogger(__name__)
SPARKX_MCP_URL = "https://mcp.sparkx.cn/mcp"
READ_TOOLS = frozenset({"get_user_authorized_context", "get_ads_perf", "get_entity_metadata"})
FACT_ENTITIES = frozenset({"campaign", "adGroup", "target", "searchTerm", "placement", "productAd", "asin"})
METADATA_ENTITIES = frozenset({"campaign", "adGroup", "target", "productAd", "portfolio"})
PAGE_SIZE, MAX_PAGES = 500, 20
_PID = re.compile(r"[1-9][0-9]{0,19}")
# The shared interpretation boundary of every successful read always mentions
# "partial or paginated results"; only explicit truncation wording is fatal.
_BOUNDARY = ("Limit conclusions to this query's scope.",
             "Missing fields are not zero, and empty results do not prove that no matching business data exists.",
             "Do not present partial or paginated results as complete.",
             "Distinguish returned facts from analytical inferences; state limitations or query further when "
             "evidence is insufficient.")
_TRUNCATED = re.compile(r"\btruncat\w*|\bcut[ -]?off\b|\bnot all rows\b|\b(?:first|only) \d+ rows\b|截断|仅返回部分")

MESSAGES = {
    "SPARKX_NOT_CONFIGURED": "尚未配置 SparkX MCP Token，请在控制台「SparkX 连接」保存并测试。",
    "SPARKX_INVALID_TOKEN_FORMAT": "SparkX MCP Token 格式无效。",
    "SPARKX_NO_PROFILES": "没有可监控的 SparkX 店铺：Token 未授权任何店铺，或店铺已全部关闭监控。",
    "SPARKX_AUTH_REJECTED": "SparkX 拒绝了该 Token（401）：Token 无效、过期、被禁用或缺少只读权限。",
    "SPARKX_TOKEN_INVALID": "SparkX 判定 Token 无效，请在 SparkX 后台重新生成后保存。",
    "SPARKX_FORBIDDEN": "SparkX 拒绝访问（403），请核对 Token 的授权范围。",
    "SPARKX_PERMISSION_DENIED": "Token 缺少所需的只读权限（广告效果 / 广告配置）。",
    "SPARKX_SCOPE_MISSING": "Token 缺少所需的只读权限（广告效果 / 广告配置）。",
    "SPARKX_PROFILE_OUT_OF_RANGE": "请求的店铺不在 Token 授权范围内，请刷新店铺列表。",
    "SPARKX_RATE_LIMITED": "SparkX 限流（每工具每分钟 120 次），请稍后重试。",
    "SPARKX_TIMEOUT": "SparkX 查询超时，请稍后重试。",
    "SPARKX_UNREACHABLE": "服务器无法连接 mcp.sparkx.cn。",
    "SPARKX_PROVIDER_ERROR": "SparkX 服务暂时异常，请稍后重试。",
    "SPARKX_SERVICE_UNAVAILABLE": "SparkX 服务暂时异常，请稍后重试。",
    "SPARKX_INVALID_PARAMS": "SparkX 拒绝了查询参数。",
    "SPARKX_QUERY_SHAPE_UNSUPPORTED": "SparkX 不支持该查询组合。",
    "SPARKX_BUSINESS_ERROR": "SparkX 返回业务错误。",
    "SPARKX_RESULT_UNREADABLE": "SparkX 返回内容无法解析。",
    "SPARKX_PAGE_NOT_PROVEN": "SparkX 分页结构无法核验，未使用该结果。",
    "SPARKX_SOURCE_TRUNCATED": "SparkX 提示结果被截断，未使用不完整数据。",
    "SPARKX_PAGINATION_LIMIT": "结果超过分页上限，未使用不完整数据。",
    "SPARKX_CURRENCY_UNCONFIRMED": "SparkX 未确认金额币种，未使用该结果。",
    "SPARKX_PROFILE_SCOPE_MISMATCH": "SparkX 返回的店铺范围与请求不一致，未使用该结果。",
    "SPARKX_ROW_SCOPE_MISMATCH": "SparkX 返回了请求范围外的行，未使用该结果。",
    "SPARKX_DUPLICATE_ROW": "SparkX 返回了重复的数据行，未使用该结果。",
    "SPARKX_METRIC_INVALID": "SparkX 返回了无效的指标值，未使用该结果。",
    "SPARKX_TRAFFIC_CONTRADICTORY": "SparkX 返回的点击多于曝光，未使用该结果。",
    "SPARKX_DATE_INVALID": "SparkX 返回了无效日期，未使用该结果。",
    "SPARKX_PROFILES_UNREADABLE": "SparkX 授权店铺列表无法解析。",
    "SPARKX_TOOL_NOT_ALLOWED": "该 SparkX 工具不在只读白名单内。",
    "SPARKX_REQUEST_FAILED": "SparkX 请求失败，请检查授权、网络和超时设置。",
}


class SparkXError(RuntimeError):
    """A bounded public code; never contains provider text or credentials."""

    def __init__(self, code, status_code=502):
        super().__init__(code)
        self.code, self.status_code = code, status_code

    @property
    def message(self):
        return message(self.code)


def message(code):
    return MESSAGES.get(code, f"SparkX 数据未能核验（{code}）。")


def failure_code(error) -> str:
    """Classify a transport failure by type and HTTP status only, never by provider text."""
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, SparkXError):
            return item.code.removeprefix("SPARKX_")
        if isinstance(item, httpx.HTTPStatusError):
            status = item.response.status_code
            return ({401: "AUTH_REJECTED", 403: "FORBIDDEN", 429: "RATE_LIMITED"}.get(status)
                    or ("PROVIDER_ERROR" if status >= 500 else "REQUEST_FAILED"))
        if isinstance(item, (httpx.TimeoutException, TimeoutError)):
            return "TIMEOUT"
        if isinstance(item, httpx.TransportError):
            return "UNREACHABLE"
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        pending.extend((item.__cause__, item.__context__))
    return "REQUEST_FAILED"


def validate_token(token) -> bool:
    return (isinstance(token, str) and 0 < len(token) <= 4096
            and all(33 <= ord(char) <= 126 for char in token))


@asynccontextmanager
async def open_session(token, timeout, *, transport=None):
    """One MCP session on the fixed vendor origin; no redirects, no proxy environment."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    if not validate_token(token):
        raise SparkXError("SPARKX_INVALID_TOKEN_FORMAT", 409)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=httpx.Timeout(timeout),
                                 follow_redirects=False, trust_env=False, transport=transport) as http:
        async with streamable_http_client(SPARKX_MCP_URL, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


def _own_error(error):
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, SparkXError):
            return item
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        pending.extend((item.__cause__, item.__context__))
    return None


def run(async_fn, *, total_timeout):
    """Run one SparkX conversation synchronously and surface only a SparkXError code."""
    async def bounded():
        with anyio.fail_after(total_timeout):
            return await async_fn()

    try:
        return anyio.run(bounded)
    except SparkXError:
        raise
    except BaseException as exc:  # noqa: BLE001 - MCP wraps failures in exception groups.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        found = _own_error(exc)
        if found is not None:
            raise SparkXError(found.code, found.status_code) from None
        _LOG.warning("SparkX conversation failed (%s)", type(exc).__name__)
        raise SparkXError("SPARKX_" + failure_code(exc)) from None


def _reject_constant(_):
    raise ValueError("invalid JSON constant")


def _view(result):
    view = result.structuredContent if isinstance(getattr(result, "structuredContent", None), dict) else None
    if view is not None:
        return view
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) != "text":
            continue
        text = block.text.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        if text.startswith("{") and len(text) <= 4 * 1024 * 1024:
            try:
                return json.loads(text, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                return None
    return None


def envelope(result, tool):
    """Structured content first; a JSON text mirror only when structure is absent."""
    view = _view(result)
    failed = bool(getattr(result, "isError", False))
    if failed or not isinstance(view, dict) or view.get("isError", False) is not False or "errorType" in view:
        kind = view.get("errorType") if isinstance(view, dict) else None
        if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,40}", kind):
            raise SparkXError("SPARKX_" + kind.upper())
        raise SparkXError("SPARKX_BUSINESS_ERROR" if failed or isinstance(view, dict)
                          else "SPARKX_RESULT_UNREADABLE")
    if view.get("toolName", tool) != tool:
        raise SparkXError("SPARKX_RESULT_UNREADABLE")
    return view


async def call(session, tool, arguments):
    if tool not in READ_TOOLS:
        raise SparkXError("SPARKX_TOOL_NOT_ALLOWED", 400)
    return envelope(await session.call_tool(tool, arguments), tool)


def plain_key(name):
    """`campaign.campaignName_` and `campaignName` both map to `campaignName`."""
    key = name.split(".", 1)[1] if "." in name else name
    return key[:-1] if key.endswith("_") else key


def num(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise SparkXError("SPARKX_METRIC_INVALID")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,24}(?:\.[0-9]{1,12})?", value.strip()):
        number = float(value.strip())
    else:
        raise SparkXError("SPARKX_METRIC_INVALID")
    if not math.isfinite(number) or number < 0:
        raise SparkXError("SPARKX_METRIC_INVALID")
    return number


def pid(value):
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, (int, str)) else ""
    return text if _PID.fullmatch(text) else None


def day(value):
    text = str(value or "")
    if re.fullmatch(r"[0-9]{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise SparkXError("SPARKX_DATE_INVALID") from None


def query_specific_hint(hint):
    if not isinstance(hint, str):
        return ""
    text = hint
    for sentence in _BOUNDARY:
        text = text.replace(sentence, " ")
    return " ".join(text.split())[:300]


def _page_meta(view, page, requested, *, currency_required):
    data, meta = view.get("data"), view.get("meta")
    if (not isinstance(data, dict) or not isinstance(data.get("rows"), list)
            or type(data.get("rowCount")) is not int or data["rowCount"] != len(data["rows"])
            or not isinstance(meta, dict) or type(meta.get("hasNextPage")) is not bool
            or meta.get("page", page) != page):
        raise SparkXError("SPARKX_PAGE_NOT_PROVEN")
    effective = meta.get("effectiveProfileIds")
    if effective is not None and (not isinstance(effective, list)
                                  or {pid(value) for value in effective} != set(requested)):
        raise SparkXError("SPARKX_PROFILE_SCOPE_MISMATCH")
    if _TRUNCATED.search(query_specific_hint(meta.get("hint")).casefold()):
        raise SparkXError("SPARKX_SOURCE_TRUNCATED")
    currency = meta.get("currency")
    if currency_required and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise SparkXError("SPARKX_CURRENCY_UNCONFIRMED")
    return data["rows"], meta["hasNextPage"], currency


async def pages(session, tool, arguments, requested, *, currency_required=True, page_size=PAGE_SIZE,
                max_pages=MAX_PAGES, pause=0.2):
    """Every page of one query, or an error: a partial result is never returned."""
    rows, currency = [], None
    for page in range(1, max_pages + 1):
        view = await call(session, tool, {**arguments, "page": page, "pageSize": page_size})
        batch, more, found = _page_meta(view, page, requested, currency_required=currency_required)
        if currency_required:
            if currency is not None and found != currency:
                raise SparkXError("SPARKX_CURRENCY_UNCONFIRMED")
            currency = found
        if not all(isinstance(row, dict) for row in batch):
            raise SparkXError("SPARKX_RESULT_UNREADABLE")
        rows.extend({plain_key(k): v for k, v in row.items()} for row in batch)
        if not more:
            return rows, currency
        if not batch:
            raise SparkXError("SPARKX_PAGE_NOT_PROVEN")
        await anyio.sleep(pause)  # Pace pages well below the 120 reads/min budget.
    raise SparkXError("SPARKX_PAGINATION_LIMIT")


async def first_page(session, tool, arguments, requested, *, page_size):
    """One page for interactive questions: (rows, currency, has_more); the caller says so when more exist."""
    view = await call(session, tool, {**arguments, "page": 1, "pageSize": page_size})
    batch, more, currency = _page_meta(view, 1, requested, currency_required=True)
    if not all(isinstance(row, dict) for row in batch):
        raise SparkXError("SPARKX_RESULT_UNREADABLE")
    return [{plain_key(k): v for k, v in row.items()} for row in batch], currency, more


async def authorized_profiles(session):
    """{profile_id: {name, country, store_type}} from get_user_authorized_context."""
    view = await call(session, "get_user_authorized_context", {})
    rows = view.get("profiles")
    if not isinstance(rows, list):
        raise SparkXError("SPARKX_PROFILES_UNREADABLE")
    profiles = {}
    for row in rows[:2000]:
        value = pid(row.get("profileId")) if isinstance(row, dict) else None
        if value is None or value in profiles:
            raise SparkXError("SPARKX_PROFILES_UNREADABLE")
        country = row.get("countryCode")
        country = country.upper() if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country) else "未知"
        name = row.get("profileName")
        store_type = row.get("storeType") if row.get("storeType") in {"seller", "vendor"} else None
        profiles[value] = {"name": name.strip()[:120] if isinstance(name, str) and name.strip() else value,
                           "country": "UK" if country == "GB" else country, "store_type": store_type}
    return profiles


# ---------------------------------------------------------------- bot query validation

_SELECT = re.compile(r"(?:date|hour|[a-z][A-Za-z]{1,30}\.[a-z][A-Za-z0-9]{1,40}_|query_|matchType_|"
                     r"targetId_|adGroupId_|campaignId_)")
_TIME_EXPRESSIONS = frozenset({
    "toMonday(parseDateTime32BestEffort(date)) as week",
    "toStartOfMonth(parseDateTime32BestEffort(date)) as month",
})
_METRIC = re.compile(r"[A-Z][A-Za-z0-9]{1,40}")
_FILTER_KEY = re.compile(r"AND|OR|p\.[A-Za-z]{2,40}_|[a-z][A-Za-z]{1,30}\.[a-z][A-Za-z0-9]{1,40}_|"
                         r"[A-Z][A-Za-z0-9]{1,40}|query_|matchType_")
_OPERATORS = frozenset({">", "<", ">=", "<=", "=", "!=", "in", "like", "ilike", "isNull", "isNotNull"})


class QueryRejected(ValueError):
    """A model-proposed query outside the read-only contract; safe to show."""


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (TypeError, ValueError):
        pass
    raise QueryRejected("日期须为 YYYY-MM-DD")


def _filters(value, depth=0):
    if depth > 4:
        raise QueryRejected("筛选条件嵌套过深")
    if isinstance(value, list):
        if len(value) > 50:
            raise QueryRejected("筛选条件过多")
        for item in value:
            _filters(item, depth + 1)
        return
    if not isinstance(value, dict) or len(value) > 12:
        raise QueryRejected("筛选条件格式无效")
    for key, item in value.items():
        if key in _OPERATORS:
            if item is None or isinstance(item, (dict, list)) and key not in {"in"}:
                raise QueryRejected("筛选运算无效")
            continue
        if not isinstance(key, str) or not _FILTER_KEY.fullmatch(key):
            raise QueryRejected("筛选字段无效")
        if item is None:
            raise QueryRejected("筛选值不能为 null")
        if isinstance(item, (dict, list)):
            _filters(item, depth + 1)
        elif not isinstance(item, (str, int, float, bool)) or isinstance(item, str) and len(item) > 200:
            raise QueryRejected("筛选值无效")


def validate_performance_query(arguments, allowed):
    """Bound a get_ads_perf call proposed by the model to the monitored profiles."""
    if not isinstance(arguments, dict):
        raise QueryRejected("查询格式无效")
    known = {"profileIds", "factEntity", "dateStart", "dateEnd", "metrics", "userContext", "select",
             "filters", "orderBy", "page", "pageSize", "queryType", "timeGranularity", "language"}
    if arguments.keys() - known or "tenantId" in arguments or "userId" in arguments:
        raise QueryRejected("包含未支持的查询参数")
    profiles = arguments.get("profileIds")
    if (not isinstance(profiles, list) or not 1 <= len(profiles) <= 200
            or any(type(item) is not int or pid(item) is None or str(item) not in allowed for item in profiles)):
        raise QueryRejected("只能查询已监控的店铺（profileIds 须来自 list_stores）")
    fact = arguments.get("factEntity")
    if fact not in FACT_ENTITIES:
        raise QueryRejected("factEntity 无效")
    start, end = _date(arguments.get("dateStart")), _date(arguments.get("dateEnd"))
    hourly = str(arguments.get("timeGranularity", "daily")).lower() == "hourly"
    if str(arguments.get("timeGranularity", "daily")).lower() not in {"daily", "hourly"}:
        raise QueryRejected("timeGranularity 只能是 daily 或 hourly")
    if hourly and fact != "campaign":
        raise QueryRejected("小时数据只支持 campaign")
    if not 0 <= (end - start).days <= (6 if hourly else 89):
        raise QueryRejected("日期跨度超限（日数据 ≤90 天，小时数据 ≤7 天）")
    metrics = arguments.get("metrics")
    if (not isinstance(metrics, list) or not 1 <= len(metrics) <= 20
            or any(not isinstance(item, str) or not _METRIC.fullmatch(item) for item in metrics)):
        raise QueryRejected("metrics 须为 1–20 个指标名")
    select = arguments.get("select", [])
    if (not isinstance(select, list) or len(select) > 10
            or any(not isinstance(item, str) or not (_SELECT.fullmatch(item) or item in _TIME_EXPRESSIONS)
                   for item in select)):
        raise QueryRejected("select 字段无效")
    if "queryType" in arguments and (fact not in {"target", "searchTerm"}
                                     or arguments["queryType"] not in {"keyword", "product", "auto"}):
        raise QueryRejected("queryType 只适用于 target / searchTerm")
    if "filters" in arguments:
        _filters(arguments["filters"])
    order = arguments.get("orderBy", [])
    if (not isinstance(order, list) or len(order) > 3
            or any(not isinstance(rule, dict) or set(rule) - {"field", "direction"}
                   or not isinstance(rule.get("field"), str) or len(rule["field"]) > 80
                   or rule.get("direction", "DESC") not in {"ASC", "DESC"} for rule in order)):
        raise QueryRejected("orderBy 无效")
    for key, maximum in (("page", 50), ("pageSize", 200)):
        if key in arguments and (type(arguments[key]) is not int or not 1 <= arguments[key] <= maximum):
            raise QueryRejected(f"{key} 超出范围")
    context = arguments.get("userContext")
    if not isinstance(context, str) or not context.strip() or len(context) > 100:
        raise QueryRejected("userContext 须为 1–100 字")
    if len(json.dumps(arguments, ensure_ascii=False)) > 16_384:
        raise QueryRejected("查询过大")


def validate_metadata_query(arguments, allowed):
    if not isinstance(arguments, dict):
        raise QueryRejected("查询格式无效")
    if arguments.keys() - {"entity", "profileIds", "filters", "select", "page", "pageSize", "userContext"}:
        raise QueryRejected("包含未支持的查询参数")
    if arguments.get("entity") not in METADATA_ENTITIES:
        raise QueryRejected("entity 无效")
    profiles = arguments.get("profileIds")
    if (not isinstance(profiles, list) or not 1 <= len(profiles) <= 50
            or any(type(item) is not int or str(item) not in allowed for item in profiles)):
        raise QueryRejected("只能查询已监控的店铺")
    filters = arguments.get("filters", {})
    if not isinstance(filters, dict) or len(filters) > 10 or any(
            not isinstance(key, str) or not re.fullmatch(r"[a-z][A-Za-z]{1,40}", key)
            or not isinstance(value, (str, int, float, list)) for key, value in filters.items()):
        raise QueryRejected("filters 无效")
    select = arguments.get("select", [])
    if not isinstance(select, list) or len(select) > 20 or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z][A-Za-z]{1,40}", item) for item in select):
        raise QueryRejected("select 无效")
    for key, maximum in (("page", 20), ("pageSize", 200)):
        if key in arguments and (type(arguments[key]) is not int or not 1 <= arguments[key] <= maximum):
            raise QueryRejected(f"{key} 超出范围")
    context = arguments.get("userContext")
    if not isinstance(context, str) or not context.strip() or len(context) > 100:
        raise QueryRejected("userContext 须为 1–100 字")
