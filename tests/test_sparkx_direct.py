import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient

from jx_alert.config import Settings
from jx_alert.sparkx_direct import (
    SPARKX_MCP_URL,
    SparkXDirectError,
    call_sparkx,
    parse_profile_ids,
    validate_performance_query,
)


PROFILE = 1234567890123456
TOKEN = "dummy-token-for-offline-tests-only"
OTHER = 2234567890123456
RELAY_TOKEN = "separate-inbound-credential-for-offline-tests"


def _query(**overrides):
    return {
        "profileIds": [PROFILE],
        "factEntity": "campaign",
        "dateStart": "2026-11-14",
        "dateEnd": "2026-11-20",
        "metrics": ["Spend", "Impressions"],
        "userContext": "JX read-only advertising performance query",
        **overrides,
    }


def test_configured_profiles_and_daily_boundary():
    assert parse_profile_ids(f"{PROFILE}, {OTHER}") == frozenset({PROFILE, OTHER})
    allowed = frozenset({PROFILE})
    end = date(2026, 11, 20)
    for granularity in ({}, {"timeGranularity": ""}):
        validate_performance_query(
            _query(factEntity="asin", dateStart=(end - timedelta(days=89)).isoformat(),
                   **granularity), allowed
        )
        with pytest.raises(SparkXDirectError):
            validate_performance_query(
                _query(factEntity="asin", dateStart=(end - timedelta(days=90)).isoformat(),
                       **granularity), allowed
            )


@pytest.mark.parametrize("value", ["not-a-profile", "0", "-1", "true"])
def test_profile_configuration_fails_closed(value):
    with pytest.raises(SparkXDirectError):
        parse_profile_ids(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"profileIds": []},
        {"profileIds": [OTHER]},
        {"profileIds": [PROFILE, OTHER]},
        {"profileIds": [True]},
        {"profileIds": [str(PROFILE)]},
        {"dateStart": "20261120"},
        {"dateStart": "2026-11-21"},
        {"dateStart": "2026-11-31"},
        {"metrics": []},
        {"metrics": ["Spend", None]},
        {"timeGranularity": None},
        {"timeGranularity": "weekly"},
        {"userContext": "x" * 101},
    ],
)
def test_invalid_queries_are_rejected(changes):
    with pytest.raises(SparkXDirectError):
        validate_performance_query(_query(**changes), frozenset({PROFILE}))


def test_query_must_specify_the_required_fields():
    query = _query()
    query.pop("dateEnd")
    with pytest.raises(SparkXDirectError):
        validate_performance_query(query, frozenset({PROFILE}))


@pytest.mark.parametrize("selection", [{}, {"select": ["date"]}, {"select": ["date", "hour"]}])
def test_hourly_campaign_scope_and_inclusive_boundary(selection):
    allowed = frozenset({PROFILE})
    query = _query(timeGranularity="hourly", **selection)
    validate_performance_query(query, allowed)
    for change in ({"factEntity": "asin"}, {"dateStart": "2026-11-13"}):
        with pytest.raises(SparkXDirectError):
            validate_performance_query({**query, **change}, allowed)


def test_keyword_placement_has_its_own_seven_day_limit():
    allowed = frozenset({PROFILE})
    validate_performance_query(_query(factEntity="keywordPlacement"), allowed)
    for change in (
        {"dateStart": "2026-11-13"},
        {"timeGranularity": "daily"},
        {"timeGranularity": "hourly"},
        {"timeGranularity": ""},
    ):
        with pytest.raises(SparkXDirectError):
            validate_performance_query(_query(factEntity="keywordPlacement", **change), allowed)


def _mock_rpc(calls, result):
    def handle(request):
        assert request.url == httpx.URL(SPARKX_MCP_URL)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.method == "POST"
        rpc = json.loads(request.content)
        calls.append(rpc)
        method = rpc["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            payload = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "SparkX fixture", "version": "1"},
            }
        elif method == "tools/list":
            payload = {
                "tools": [
                    {
                        "name": name,
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                    for name in ("get_ads_perf", "get_user_authorized_context")
                ],
            }
        else:
            assert method == "tools/call"
            payload = result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": payload})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"timeGranularity": "hourly"},
        {"timeGranularity": "Hourly", "select": ["date"]},
        {"timeGranularity": ""},
    ],
)
def test_sdk_handshake_and_raw_upstream_result_are_preserved(overrides):
    calls = []
    payload = {
        "content": [{"type": "text", "text": "# SparkX 原文\n\n| 花费 |\n| --- |\n| 1.20 |"}],
        "structuredContent": {"data": {"rows": [{"Spend": 1.2, "Conversions": None}]}},
        "isError": False,
    }
    query = _query(**overrides)
    validate_performance_query(query, frozenset({PROFILE}))
    result = asyncio.run(call_sparkx(
        "get_ads_perf", query, token=TOKEN, transport=_mock_rpc(calls, payload)
    ))
    assert [r["method"] for r in calls[:2]] == ["initialize", "notifications/initialized"]
    request = next(call for call in calls if call["method"] == "tools/call")
    assert request["params"] == {"name": "get_ads_perf", "arguments": query}
    assert result.model_dump(mode="json", by_alias=True, exclude_none=True) == payload


def test_upstream_tool_error_stays_an_error_not_a_success_or_local_fallback():
    calls = []
    payload = {
        "content": [{"type": "text", "text": "SparkX: report not available"}],
        "structuredContent": {"errorType": "fixture_error"},
        "isError": True,
    }
    result = asyncio.run(call_sparkx(
        "get_ads_perf", _query(), token=TOKEN, transport=_mock_rpc(calls, payload)
    ))
    assert result.isError is True
    assert result.structuredContent == payload["structuredContent"]
    assert result.content[0].text == payload["content"][0]["text"]


@pytest.mark.parametrize("name", ["update_campaign", "create_ai_group", "unknown_tool"])
def test_unsafe_or_unknown_tools_do_not_reach_the_network(name):
    def deny_network(_request):
        pytest.fail("Unsupported SparkX tool must not reach the network")

    with pytest.raises(SparkXDirectError):
        asyncio.run(call_sparkx(name, {}, token=TOKEN, transport=httpx.MockTransport(deny_network)))


def test_empty_token_does_not_reach_the_network():
    def deny_network(_request):
        pytest.fail("Unconfigured SparkX token must not reach the network")

    with pytest.raises(SparkXDirectError):
        asyncio.run(call_sparkx(
            "get_user_authorized_context", {}, token="", transport=httpx.MockTransport(deny_network)
        ))


@pytest.mark.parametrize("failure", ["http", "redirect", "timeout"])
def test_transport_errors_do_not_disclose_credentials_or_follow_redirects(failure):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        assert request.url == httpx.URL(SPARKX_MCP_URL)
        if failure == "timeout":
            raise httpx.ReadTimeout(f"diagnostic includes {TOKEN}", request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.example/mcp"})
        return httpx.Response(401, text=f"diagnostic includes {TOKEN}")

    with pytest.raises(SparkXDirectError) as raised:
        asyncio.run(call_sparkx(
            "get_user_authorized_context", {}, token=TOKEN, transport=httpx.MockTransport(handle)
        ))
    assert TOKEN not in str(raised.value)
    assert calls and set(calls) == {SPARKX_MCP_URL}


def _relay_settings():
    return Settings(
        _env_file=None,
        mcp_output_mode="sparkx_direct",
        mcp_bearer_token="old-jx-bearer-credential-for-offline-tests",
        sparkx_profile_ids=str(PROFILE),
        sparkx_ai_token=TOKEN,
        sparkx_relay_bearer_token=RELAY_TOKEN,
    )


def test_scoped_fastmcp_exposes_only_allowed_read_and_returns_call_tool_result(monkeypatch):
    from jx_alert import sparkx_direct
    from jx_alert.mcp_server import build_sparkx_mcp

    raw = CallToolResult(
        content=[TextContent(type="text", text="### 原样\n\n| 账户 | 花费 |")],
        structuredContent={"data": {"rows": [{"Spend": 12, "ACOS": None}]}},
    )
    calls = []

    async def fake_sparkx(name, arguments, *, token):
        calls.append((name, arguments, token))
        return raw

    monkeypatch.setattr(sparkx_direct, "call_sparkx", fake_sparkx)
    async def run():
        server = build_sparkx_mcp(_relay_settings())
        exported = await server.list_tools()
        assert [tool.name for tool in exported] == ["get_ads_perf"]
        assert exported[0].annotations.readOnlyHint is True
        rejected = await server.call_tool("get_ads_perf", {"arguments": _query(profileIds=[OTHER])})
        assert rejected.isError is True
        assert calls == []
        result = await server.call_tool("get_ads_perf", {"arguments": _query()})
        assert result == raw
        assert calls == [("get_ads_perf", _query(), TOKEN)]

    asyncio.run(run())


def test_sparkx_http_accepts_only_the_distinct_relay_credential(monkeypatch):
    from jx_alert import mcp_server

    settings = _relay_settings()
    monkeypatch.setattr(mcp_server, "_MCP_SETTINGS", settings)
    message = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "offline authorization test", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(mcp_server.create_http_app(settings), base_url="http://localhost:8001") as client:
        for token in ("", TOKEN, settings.mcp_bearer_token):
            auth = {**headers, "Authorization": f"Bearer {token}"}
            refused = client.post(settings.mcp_path, json=message, headers=auth)
            assert refused.status_code == 401
        accepted = client.post(
            settings.mcp_path, json=message, headers={
                **headers, "Authorization": f"Bearer {RELAY_TOKEN}",
            },
        )
        assert accepted.status_code == 200
        assert "JX • SparkXAI direct" in accepted.text
        assert TOKEN not in accepted.text
