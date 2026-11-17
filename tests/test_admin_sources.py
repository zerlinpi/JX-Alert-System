"""Credential console routes reuse owner sessions, same-origin and CSRF checks."""
import json
from pathlib import Path

import pytest

from test_admin_console import ORIGIN, console, login  # noqa: F401
from jx_alert.lingxing.source_credentials import MCP_URL


@pytest.fixture
def source_console(console, tmp_path):  # noqa: F811 - imported fixture reused by pytest
    client, _, _, settings = console
    settings.lingxing_sources_file = str(tmp_path / "private" / "connections.json")
    settings.lingxing_business_credentials_file = str(tmp_path / "legacy-business.json")
    return client, settings


def test_sources_require_owner_session_and_csrf_before_receiving_secrets(source_console):
    client, settings = source_console
    path = "/console/api/data-sources"
    body = {"revision": "0" * 64, "kind": "mcp", "expires_on": None,
            "credentials": {"url": MCP_URL, "key": "synthetic-private-mcp-key-12345"}}
    assert client.get(path).status_code == 401
    assert client.post(path, json=body).status_code == 401
    assert login(client).status_code == 303
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 403
    csrf = client.get("/console/api/stores").json()["csrf"]
    assert client.post(path, json=body, headers={"Origin": "https://other.example", "X-CSRF-Token": csrf}).status_code == 403
    assert not Path(settings.lingxing_sources_file).exists()


def test_source_save_returns_only_metadata_and_conflicts_do_not_rotate_again(source_console):
    client, settings = source_console
    assert login(client).status_code == 303
    csrf = client.get("/console/api/stores").json()["csrf"]
    path = "/console/api/data-sources"
    revision = client.get(path).json()["revision"]
    key = "synthetic-private-mcp-key-12345"
    body = {"revision": revision, "kind": "mcp", "expires_on": None, "credentials": {"url": MCP_URL, "key": key}}
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200
    assert result.json()["mcp"]["configured"] is True
    assert key not in result.text and '"credentials"' not in result.text
    assert result.json()["events"][0]["action"] == "ADD"
    original = Path(settings.lingxing_sources_file).read_bytes()
    assert client.post(path, json=body, headers=headers).status_code == 409
    assert Path(settings.lingxing_sources_file).read_bytes() == original
    reread = client.get(path)
    assert key not in reread.text and reread.headers["cache-control"] == "no-store"
    assert json.loads(original)["sources"]["mcp"]["credentials"]["key"] == key


@pytest.mark.parametrize("extra", [{"secret_path": "/tmp/override"}, {"confirmed": True}])
def test_source_route_rejects_unrecognized_payload_fields(source_console, extra):
    client, settings = source_console
    login(client)
    csrf = client.get("/console/api/stores").json()["csrf"]
    body = {"revision": "0" * 64, "kind": "mcp", "expires_on": None,
            "credentials": {"url": MCP_URL, "key": "synthetic-private-mcp-key-12345"}, **extra}
    assert client.post("/console/api/data-sources", json=body,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 400
    assert not Path(settings.lingxing_sources_file).exists()
