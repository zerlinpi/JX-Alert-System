"""Exercise import-time FastMCP authentication wiring in isolated processes.

All credentials are synthetic, both databases are temporary, and TestClient
uses only in-process ASGI calls. No .env or external services are consulted.
"""
import os
from pathlib import Path
import subprocess
import sys


_CHILD = r'''
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select
from starlette.testclient import TestClient

from jx_alert import config
from jx_alert.config import Settings
from jx_alert.mcp_oauth import create_owner_credentials

directory = Path(sys.argv[1])
mode = sys.argv[2]
owner_file = directory / "owner.json"
password = "Synthetic integration owner password 123"
create_owner_credentials(owner_file, password)
origin = "https://jx.integration.test"
resource = origin + "/mcp"
static_token = "s" * 64
options = dict(
    _env_file=None, database_url="sqlite:///" + str(directory / "business.sqlite"),
    mcp_public_url=origin, mcp_allowed_hosts="jx.integration.test",
    mcp_allowed_origins=origin, mcp_bearer_token=static_token,
    mcp_owner_credentials_file=str(owner_file),
    mcp_oauth_database_path=str(directory / "oauth.sqlite"),
    mcp_operations_enabled=True, app_dry_run=False, scheduler_enabled=False,
    runtime_settings_file=str(directory / "no-runtime-settings.json"),
    mcp_auth_file=str(directory / "no-bearer-file.json"),
    collection_scope_file=str(directory / "no-collection-scope.json"),
    store_timezones_file=str(directory / "no-store-timezones.json"),
    feishu_routing_file=str(directory / "no-feishu-routing.json"),
    feishu_app_credentials_file=str(directory / "no-feishu-credentials.json"),
    lingxing_token_store_path=str(directory / "no-lingxing-token.json"),
)
if mode == "oauth":
    options["mcp_auth_mode"] = "oauth"
settings = Settings(**options)
if mode == "bearer":
    assert settings.mcp_auth_mode == "bearer", "Bearer must remain the default"
config.get_settings = lambda: settings

# Import only after the isolated Settings object is installed. This matters:
# FastMCP and its auth provider are constructed when mcp_server is imported.
from jx_alert import mcp_server
from jx_alert.storage.db import SessionLocal, init_db
from jx_alert.storage.models import Store, SyncJob

init_db()
with SessionLocal() as db:
    db.add(Store(sid="synthetic-store", name="Synthetic integration store", status=1,
                 timezone="America/Los_Angeles", currency_code="USD"))
    db.commit()

def job_count():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(SyncJob))

app = mcp_server.create_http_app(settings)

def decoded(response):
    assert response.status_code == 200, (response.status_code, response.text)
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    # Streamable HTTP may return the response as an SSE event rather than JSON.
    events = [json.loads(line[5:].strip()) for line in response.text.splitlines()
              if line.startswith("data:")]
    assert events, response.text
    return next(item for item in events if "id" in item)

def protocol_headers(token):
    return {"Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}

def initialize(client, token):
    headers = protocol_headers(token)
    response = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "jx-local-integration", "version": "1"}},
    })
    result = decoded(response)
    assert result["result"]["serverInfo"]["name"] == "JX Alert System"
    assert "tools" in result["result"]["capabilities"]
    if response.headers.get("mcp-session-id"):
        headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
    headers["Mcp-Protocol-Version"] = result["result"]["protocolVersion"]
    notified = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    })
    assert notified.status_code == 202, notified.text
    return headers

def rpc(client, headers, method, params, request_id=2):
    return decoded(client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
    }))

def call(client, headers, name, arguments=None):
    result = rpc(client, headers, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in result, result
    return result["result"]

def owner_token(client, scopes, account=None):
    callback = "https://chatgpt.com/connector/oauth/synthetic-integration"
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    if account is None:
        registration = client.post("/register", json={
            "redirect_uris": [callback], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": " ".join(scopes),
        })
        assert registration.status_code == 201, registration.text
        account = registration.json()
    authorization = client.get("/authorize", params={
        "client_id": account["client_id"], "redirect_uri": callback,
        "response_type": "code", "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": resource,
        "scope": " ".join(scopes), "state": "synthetic-state",
    })
    assert authorization.status_code == 302, authorization.text
    page = client.get(authorization.headers["location"])
    assert page.status_code == 200, page.text
    request_id = re.search(r'name="request" value="([^"]+)"', page.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    login = client.post("/oauth/owner", headers={"Origin": origin}, data={
        "request": request_id, "csrf": csrf, "action": "login", "password": password,
    })
    assert login.status_code == 200 and "同意" in login.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
    consent = client.post("/oauth/owner", headers={"Origin": origin}, data={
        "request": request_id, "csrf": csrf, "action": "approve",
    })
    assert consent.status_code == 302, consent.text
    query = parse_qs(urlsplit(consent.headers["location"]).query)
    assert query["state"] == ["synthetic-state"]
    token = client.post("/token", data={
        "grant_type": "authorization_code", "client_id": account["client_id"],
        "code": query["code"][0], "redirect_uri": callback,
        "code_verifier": verifier, "resource": resource,
    })
    assert token.status_code == 200, token.text
    return token.json()["access_token"], account

with TestClient(app, base_url=origin, follow_redirects=False) as client:
    anonymous = client.get("/mcp", headers={"Accept": "application/json, text/event-stream"})
    assert anonymous.status_code == 401
    assert job_count() == 0
    if mode == "oauth":
        assert "resource_metadata" in anonymous.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
        assert metadata.json()["scopes_supported"] == ["jx:read", "jx:collect", "jx:notify"]
        protected = client.get("/.well-known/oauth-protected-resource/mcp")
        assert protected.status_code == 200, protected.text
        assert protected.json()["resource"] == resource
        assert client.get("/mcp", headers=protocol_headers(static_token)).status_code == 401

        read_token, account = owner_token(client, ["jx:read"])
        read_headers = initialize(client, read_token)
        listing = rpc(client, read_headers, "tools/list", {})["result"]["tools"]
        by_name = {tool["name"]: tool for tool in listing}
        assert {"get_stores", "get_configuration_status", "request_collection"} <= by_name.keys()
        assert by_name["get_stores"]["annotations"]["readOnlyHint"] is True
        assert by_name["request_collection"]["annotations"]["readOnlyHint"] is False
        assert by_name["request_collection"]["_meta"]["securitySchemes"][0]["scopes"] == ["jx:read", "jx:collect"]
        assert by_name["submit_chat_analysis"]["_meta"]["securitySchemes"][0]["scopes"] == ["jx:read", "jx:notify"]
        assert by_name["submit_advertising_kpi_analysis"]["_meta"]["securitySchemes"][0]["scopes"] == ["jx:read", "jx:notify"]
        assert by_name["prepare_advertising_kpi_analysis"]["_meta"]["securitySchemes"][0]["scopes"] == ["jx:read"]
        workflow_scopes = ["jx:read", "jx:collect", "jx:notify"]
        permission_tool = by_name["check_workflow_permissions"]
        assert permission_tool["annotations"]["readOnlyHint"] is True
        assert permission_tool["_meta"]["securitySchemes"][0]["scopes"] == workflow_scopes
        preflight = call(client, read_headers, "check_workflow_permissions")
        assert preflight["isError"] is True
        assert 'scope="jx:read jx:collect jx:notify"' in preflight["_meta"]["mcp/www_authenticate"][0]
        stores = call(client, read_headers, "get_stores")
        assert not stores.get("isError") and "Synthetic integration store" in json.dumps(stores)
        configuration = call(client, read_headers, "get_configuration_status")
        assert not configuration.get("isError"), configuration
        denied = call(client, read_headers, "request_collection", {
            "backfill_days": 1, "idempotency_key": "must-not-enqueue",
        })
        assert denied.get("isError") is True and "jx:collect" in json.dumps(denied)
        assert 'error="insufficient_scope"' in denied["_meta"]["mcp/www_authenticate"][0]
        denied_notification = call(client, read_headers, "submit_chat_analysis", {"review_id": 1, "findings": []})
        assert denied_notification.get("isError") is True and "jx:notify" in json.dumps(denied_notification)
        assert 'scope="jx:read jx:notify"' in denied_notification["_meta"]["mcp/www_authenticate"][0]
        denied_kpi = call(client, read_headers, "submit_advertising_kpi_analysis", {"review_id": 1, "findings": []})
        assert denied_kpi.get("isError") is True and "jx:notify" in json.dumps(denied_kpi)
        assert job_count() == 0, "Read-only scope must never enqueue a job"

        notify_token, _ = owner_token(client, ["jx:read", "jx:notify"], account)
        notify_headers = initialize(client, notify_token)
        missing_collect = call(client, notify_headers, "request_collection")
        assert 'scope="jx:read jx:collect jx:notify"' in missing_collect["_meta"]["mcp/www_authenticate"][0]
        assert job_count() == 0

        collect_token, _ = owner_token(client, workflow_scopes, account)
        collect_headers = initialize(client, collect_token)
        ready = call(client, collect_headers, "check_workflow_permissions")
        assert not ready.get("isError") and "AUTHORIZED" in json.dumps(ready)
        assert job_count() == 0
        queued = call(client, collect_headers, "request_collection", {
            "backfill_days": 1, "idempotency_key": "permitted-collection",
        })
        assert not queued.get("isError"), queued
        assert job_count() == 1
        repeated = call(client, collect_headers, "request_collection", {
            "backfill_days": 1, "idempotency_key": "permitted-collection",
        })
        assert not repeated.get("isError"), repeated
        assert job_count() == 1
        with SessionLocal() as db:
            job = db.scalar(select(SyncJob))
            assert job.status == "PENDING", "The tool queues work without running the collector"
    else:
        assert anonymous.headers["www-authenticate"] == "Bearer"
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert client.get("/mcp", headers=protocol_headers("incorrect")).status_code == 401
        headers = initialize(client, static_token)
        stores = call(client, headers, "get_stores")
        assert not stores.get("isError") and "Synthetic integration store" in json.dumps(stores)
        assert job_count() == 0
print("integration:ok")
'''


def _isolated_run(tmp_path, mode):
    # Preserve interpreter/OS essentials, never the caller's database, Lingxing,
    # OAuth, Feishu or other application credentials from its environment.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
               "APPDATA", "LOCALAPPDATA", "PYTHONHOME", "LD_LIBRARY_PATH"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), mode],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "integration:ok" in completed.stdout


def test_real_streamable_http_oauth_flow_and_per_tool_collection_scope(tmp_path):
    _isolated_run(tmp_path, "oauth")


def test_default_static_bearer_transport_remains_compatible(tmp_path):
    _isolated_run(tmp_path, "bearer")
