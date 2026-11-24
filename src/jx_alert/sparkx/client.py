"""Read-only SparkX AI MCP client (Streamable HTTP, Bearer MCP token).

Contract: https://github.com/SparkXAI/SparkXAI-MCP. DeepSeek uses SparkX the way
an MCP agent does: it receives the read tools exactly as the server lists them
(name, description, input schema) and their results unmodified. A tool is
offered only when it is in READ_TOOLS *and* SparkX marks it readOnlyHint=true,
so write tools (batch_update_ads, create_*, save_*, delete_*) are unreachable.
Failures carry a bounded code; the token never leaves this module.
"""
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import re

import anyio
import httpx

_LOG = logging.getLogger(__name__)
SPARKX_MCP_URL = "https://mcp.sparkx.cn/mcp"
# Read-only data tools of SparkX AI MCP v1.3.x. Skill-platform tools are not offered.
READ_TOOLS = frozenset({"get_user_authorized_context", "get_ads_perf", "get_entity_metadata", "get_operation_log",
                        "get_amc_audience_perf", "get_ai_group_template"})
_PID = re.compile(r"[1-9][0-9]{0,19}")

MESSAGES = {
    "SPARKX_NOT_CONFIGURED": "尚未配置 SparkX MCP Token，请在控制台「SparkX 连接」保存并测试。",
    "SPARKX_INVALID_TOKEN_FORMAT": "SparkX MCP Token 格式无效。",
    "SPARKX_NO_PROFILES": "没有可监控的 SparkX 店铺：Token 未授权任何店铺，或店铺已全部关闭监控。",
    "SPARKX_AUTH_REJECTED": "SparkX 拒绝了该 Token（401）：Token 无效、过期、被禁用或缺少只读权限。",
    "SPARKX_TOKEN_INVALID": "SparkX 判定 Token 无效，请在 SparkX 后台重新生成后保存。",
    "SPARKX_FORBIDDEN": "SparkX 拒绝访问（403），请核对 Token 的授权范围。",
    "SPARKX_PERMISSION_DENIED": "Token 缺少所需的只读权限（广告效果 / 广告配置 / 操作日志）。",
    "SPARKX_SCOPE_MISSING": "Token 缺少所需的只读权限（广告效果 / 广告配置 / 操作日志）。",
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
    "SPARKX_PROFILES_UNREADABLE": "SparkX 授权店铺列表无法解析。",
    "SPARKX_TOOL_NOT_ALLOWED": "该 SparkX 工具不在只读白名单内。",
    "SPARKX_TOOLS_UNAVAILABLE": "SparkX 没有提供只读查询工具，请核对 Token 权限。",
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
    return MESSAGES.get(code, f"SparkX 请求未完成（{code}）。")


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
    """One MCP session on the fixed vendor origin; no redirects, no proxy environment.

    The server's own instructions (InitializeResult.instructions) are kept on the
    session as ``jx_instructions`` so the agent can pass them on verbatim.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    if not validate_token(token):
        raise SparkXError("SPARKX_INVALID_TOKEN_FORMAT", 409)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=httpx.Timeout(timeout),
                                 follow_redirects=False, trust_env=False, transport=transport) as http:
        async with streamable_http_client(SPARKX_MCP_URL, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                initialized = await session.initialize()
                session.jx_instructions = getattr(initialized, "instructions", None) or ""
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


def _foreign_error(error, kinds):
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, kinds):
            return item
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        pending.extend((item.__cause__, item.__context__))
    return None


def run(async_fn, *, total_timeout, passthrough=()):
    """Run one SparkX conversation synchronously and surface only a SparkXError code.

    Exceptions of the ``passthrough`` types (e.g. DeepSeekError inside an agent run)
    are re-raised unchanged, even when the MCP task group wrapped them.
    """
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
        if passthrough and isinstance(exc, passthrough):
            raise
        found = _own_error(exc)
        if found is not None:
            raise SparkXError(found.code, found.status_code) from None
        if passthrough and (foreign := _foreign_error(exc, passthrough)) is not None:
            raise foreign from None
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
    """A parsed read for the system's own bookkeeping (the store roster)."""
    if tool not in READ_TOOLS:
        raise SparkXError("SPARKX_TOOL_NOT_ALLOWED", 400)
    return envelope(await session.call_tool(tool, arguments), tool)


# ---------------------------------------------------------------- agent access

def read_only(tool) -> bool:
    annotations = getattr(tool, "annotations", None)
    return tool.name in READ_TOOLS and getattr(annotations, "readOnlyHint", None) is True


async def agent_tools(session):
    """The read tools exactly as SparkX lists them; anything else is never offered."""
    found, cursor, seen = [], None, set()
    for _ in range(20):
        listed = await (session.list_tools(cursor=cursor) if cursor else session.list_tools())
        found += [tool for tool in listed.tools if read_only(tool)]
        cursor = getattr(listed, "nextCursor", None)
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    if not found:
        raise SparkXError("SPARKX_TOOLS_UNAVAILABLE", 409)
    return found


async def call_raw(session, tool, arguments):
    """(text, is_error): the tool result as SparkX returned it, for the model to read.

    A JSON-RPC error from SparkX is returned as text too, as MCP clients show it to
    the agent; transport failures propagate and end the run.
    """
    from mcp.shared.exceptions import McpError

    if tool not in READ_TOOLS:
        raise SparkXError("SPARKX_TOOL_NOT_ALLOWED", 400)
    try:
        result = await session.call_tool(tool, arguments)
    except McpError as exc:
        detail = getattr(getattr(exc, "error", None), "message", None) or "request rejected"
        return f"MCP error: {str(detail)[:2000]}", True
    texts = [block.text for block in (getattr(result, "content", None) or [])
             if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)]
    text = "\n".join(texts)
    if not text and isinstance(getattr(result, "structuredContent", None), dict):
        text = json.dumps(result.structuredContent, ensure_ascii=False)
    return text, bool(getattr(result, "isError", False))


# ---------------------------------------------------------------- store roster

def pid(value):
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, (int, str)) else ""
    return text if _PID.fullmatch(text) else None


def _code(value, pattern, upper=True):
    if not isinstance(value, str) or not re.fullmatch(pattern, value.strip()):
        return None
    return value.strip().upper() if upper else value.strip()


async def authorized_profiles(session):
    """{profile_id: {name, country, store_type, currency, timezone}} from get_user_authorized_context."""
    view = await call(session, "get_user_authorized_context", {})
    data = view.get("data") if isinstance(view.get("data"), dict) else {}
    # The documented envelope nests the list under data; older responses carried it at the top level.
    rows = data.get("profiles") if "profiles" in data else view.get("profiles")
    if not isinstance(rows, list):
        raise SparkXError("SPARKX_PROFILES_UNREADABLE")
    profiles = {}
    for row in rows[:2000]:
        value = pid(row.get("profileId")) if isinstance(row, dict) else None
        if value is None or value in profiles:
            raise SparkXError("SPARKX_PROFILES_UNREADABLE")
        country = _code(row.get("countryCode"), r"[A-Za-z]{2}") or "未知"
        name = row.get("profileName")
        store_type = row.get("storeType") if row.get("storeType") in {"seller", "vendor"} else None
        profiles[value] = {"name": name.strip()[:120] if isinstance(name, str) and name.strip() else value,
                           "country": "UK" if country == "GB" else country, "store_type": store_type,
                           "currency": _code(row.get("currencyCode"), r"[A-Za-z]{3}"),
                           "timezone": _code(row.get("timezone"), r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}", upper=False)}
    return profiles


def fetch_roster(settings, *, token=None, timeout=20.0):
    """Authorized profiles only (no metrics), for the console store directory."""
    from jx_alert.sparkx.credentials import effective_token

    token = effective_token(settings) if token is None else token
    if not token:
        raise SparkXError("SPARKX_NOT_CONFIGURED", 409)

    async def conversation():
        async with open_session(token, timeout) as session:
            return await authorized_profiles(session)

    return run(conversation, total_timeout=timeout + 10)
