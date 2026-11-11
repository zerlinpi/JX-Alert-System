import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from jx_alert.config import Settings
from jx_alert.mcp_server import BearerTokenMiddleware, create_http_app


def _endpoint(request):
    return PlainTextResponse("mcp endpoint")


def _app():
    app = Starlette(routes=[Route("/mcp", _endpoint, methods=["GET", "POST"])])
    return BearerTokenMiddleware(app, "a" * 64)


def _request(app, method: str, path: str, headers=None):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(run())


def test_remote_mcp_rejects_missing_bearer_token():
    response = _request(_app(), "GET", "/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_remote_mcp_rejects_invalid_bearer_token():
    response = _request(
        _app(), "GET", "/mcp", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_remote_mcp_accepts_valid_bearer_token():
    response = _request(
        _app(), "GET", "/mcp",
        headers={"Authorization": "Bearer " + "a" * 64},
    )
    assert response.status_code == 200
    assert response.text == "mcp endpoint"


def test_remote_mcp_rejects_duplicate_authorization_headers():
    response = _request(
        _app(),
        "GET",
        "/mcp",
        headers=[
            ("Authorization", "Bearer " + "a" * 64),
            ("Authorization", "Bearer " + "a" * 64),
        ],
    )
    assert response.status_code == 401


def test_remote_mcp_does_not_protect_unrelated_paths():
    response = _request(_app(), "GET", "/not-mcp")
    assert response.status_code == 404


def test_create_http_app_requires_bearer_token():
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        create_http_app(Settings(mcp_bearer_token=""))


def test_create_http_app_builds_streamable_http_server():
    app = create_http_app(Settings(mcp_bearer_token="b" * 64))
    response = _request(app, "GET", "/mcp")
    assert response.status_code == 401


def test_remote_mcp_cors_preflight_allows_local_inspector_origin():
    app = create_http_app(Settings(mcp_bearer_token="c" * 64))
    response = _request(
        app,
        "OPTIONS",
        "/mcp",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,content-type,mcp-protocol-version"
            ),
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
