from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from jx_alert.config import Settings
from jx_alert.mcp_server import BearerTokenMiddleware, create_http_app


def _endpoint(request):
    return PlainTextResponse("mcp endpoint")


def _client():
    app = Starlette(routes=[Route("/mcp", _endpoint, methods=["GET", "POST"])])
    return TestClient(BearerTokenMiddleware(app, "a" * 64))


def test_remote_mcp_rejects_missing_bearer_token():
    response = _client().get("/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_remote_mcp_rejects_invalid_bearer_token():
    response = _client().get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_remote_mcp_accepts_valid_bearer_token():
    response = _client().get("/mcp", headers={"Authorization": "Bearer " + "a" * 64})
    assert response.status_code == 200
    assert response.text == "mcp endpoint"


def test_remote_mcp_rejects_duplicate_authorization_headers():
    response = _client().get(
        "/mcp",
        headers=[
            ("Authorization", "Bearer " + "a" * 64),
            ("Authorization", "Bearer " + "a" * 64),
        ],
    )
    assert response.status_code == 401


def test_remote_mcp_does_not_protect_unrelated_paths():
    response = _client().get("/not-mcp")
    assert response.status_code == 404



def test_create_http_app_requires_bearer_token():
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        create_http_app(Settings(mcp_bearer_token=""))


def test_create_http_app_builds_streamable_http_server():
    app = create_http_app(Settings(mcp_bearer_token="b" * 64))
    client = TestClient(app)
    response = client.get("/mcp")
    assert response.status_code == 401
