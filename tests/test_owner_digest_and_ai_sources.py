"""Owner-console controls for the 08:30 digest recipient, operator names, DeepSeek and SparkX."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat

import httpx
import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr

from test_admin_console import ORIGIN, console, login  # noqa: F401
from jx_alert import sparkx_config, sparkx_direct
from jx_alert.ai import deepseek_config
from jx_alert.notifications import company_executive


def _headers(client):
    assert login(client).status_code == 303
    csrf = client.get("/console/api/stores").json()["csrf"]
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


@pytest.fixture
def owner(console):  # noqa: F811 - imported fixture reused by pytest
    client, routing, verified, settings = console
    settings.feishu_app_id = "cli_console_app"
    settings.feishu_app_secret = SecretStr("synthetic-console-secret")
    return client, routing, settings


def test_company_digest_recipient_is_owner_only_verified_and_bound_to_app(owner):
    client, routing, settings = owner
    assert client.get("/console/api/company-digest").status_code == 401
    headers = _headers(client)
    view = client.get("/console/api/company-digest").json()
    assert view["recipient"] is None and view["status"]["enabled"] is False
    body = {"open_ids": ["ou_new"], "revision": view["revision"]}
    assert client.post("/console/api/company-digest", json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/console/api/company-digest", json={**body, "open_ids": ["ou_stranger"]},
                       headers=headers).status_code == 400
    saved = client.post("/console/api/company-digest", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["recipient"] == {"id": "ou_new", "name": "新负责人"}
    assert "cli_console_app" not in saved.text and "synthetic-console-secret" not in saved.text
    binding = json.loads(routing.read_text(encoding="utf-8"))["company_executive_digest"]
    assert binding["open_id"] == "ou_new" and binding["app_id"] == "cli_console_app"
    assert len(binding["generation"]) == 32 and datetime.fromisoformat(binding["verified_at"]).tzinfo
    # Unchanged selection keeps the same generation (no re-authorization churn).
    again = client.post("/console/api/company-digest", json={**body, "revision": saved.json()["revision"]},
                        headers=headers)
    assert json.loads(routing.read_text(encoding="utf-8"))["company_executive_digest"] == binding
    # The saved binding is exactly what the 08:30 lane accepts once the lane is enabled.
    settings.feishu_company_executive_digest_enabled = True
    settings.feishu_company_executive_digest_start_date = "2026-01-01"
    settings.scheduler_enabled, settings.app_dry_run, settings.feishu_enabled = True, False, True
    route = company_executive._route(settings, datetime.now(timezone.utc) + timedelta(seconds=1))
    assert route["receive_id"] == "ou_new" and route["generation"] == binding["generation"]
    settings.feishu_app_id = "cli_rotated_app"
    assert company_executive._route(settings, datetime.now(timezone.utc)) is None
    cleared = client.post("/console/api/company-digest",
                          json={"open_ids": [], "revision": again.json()["revision"]}, headers=headers)
    assert cleared.status_code == 200 and cleared.json()["recipient"] is None
    assert "company_executive_digest" not in json.loads(routing.read_text(encoding="utf-8"))
    assert list(routing.parent.glob("routing.json.before-console-*-company-digest-*.json"))


def test_operator_name_sync_changes_only_display_names(owner):
    client, routing, _ = owner
    headers = _headers(client)
    data = json.loads(routing.read_text(encoding="utf-8"))
    data["stores"]["5175"] = {"open_ids": ["ou_new"], "chat_id": "ops"}
    routing.write_text(json.dumps(data), encoding="utf-8")
    result = client.post("/console/api/owners/sync-names", json={}, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["updated_store_count"] == 1
    assert result.json()["unresolved_owner_count"] == 1  # ou_old is not in the group roster
    after = json.loads(routing.read_text(encoding="utf-8"))
    assert after["stores"]["5175"] == {"open_ids": ["ou_new"], "chat_id": "ops",
                                       "owner_display_names": {"ou_new": "新负责人"}}
    assert after["stores"]["5174"]["open_ids"] == ["ou_old"]
    assert client.post("/console/api/owners/sync-names", json={"all": True}, headers=headers).status_code == 400
    assert client.post("/console/api/owners/sync-names", json={}, headers=headers).json()["updated_store_count"] == 0


def _deepseek_transport(calls, *, models=("deepseek-flash", "deepseek-v4-pro"), status=200, balance=True):
    def handle(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer synthetic-deepseek-key"
        if request.url.path == "/models":
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "provider text"}})
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": model, "object": "model", "owned_by": "deepseek"} for model in models]})
        if request.url.path == "/user/balance":
            return httpx.Response(200, json={"is_available": balance, "balance_infos": []})
        raise AssertionError("connection test must not call " + request.url.path)
    return httpx.MockTransport(handle)


@pytest.mark.parametrize("case,expected", [
    ("ok", "OK"), ("unauthorized", "INVALID_KEY"), ("model", "MODEL_UNAVAILABLE"),
    ("balance", "INSUFFICIENT_BALANCE"), ("missing", "NOT_CONFIGURED"),
])
def test_deepseek_connection_check_is_free_and_returns_codes_only(tmp_path, case, expected):
    from jx_alert.config import Settings

    settings = Settings(_env_file=None, deepseek_credentials_file=str(tmp_path / "deepseek.json"),
                        deepseek_api_key=SecretStr("" if case == "missing" else "synthetic-deepseek-key"))
    calls = []
    transport = _deepseek_transport(calls, status=401 if case == "unauthorized" else 200,
                                    models=("deepseek-v4-pro",) if case == "model" else ("deepseek-flash",),
                                    balance=case != "balance")
    result = deepseek_config.check_connection(settings, transport=transport)
    assert result["status"] == expected
    assert "/chat/completions" not in calls
    assert "synthetic-deepseek-key" not in json.dumps(result) and "provider text" not in json.dumps(result)
    projection = deepseek_config.configuration(settings)
    assert projection["key_source"] == (None if case == "missing" else "environment")
    assert projection["model_source"] == "environment"


def test_deepseek_test_route_requires_csrf_and_rejects_parameters(owner, monkeypatch):
    client, _, _ = owner
    monkeypatch.setattr(deepseek_config, "check_connection", lambda settings: {"status": "OK"})
    assert client.post("/console/api/deepseek/test", json={}).status_code == 401
    headers = _headers(client)
    assert client.post("/console/api/deepseek/test", json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/console/api/deepseek/test", json={"api_key": "x"}, headers=headers).status_code == 400
    assert client.post("/console/api/deepseek/test", json={}, headers=headers).json() == {"status": "OK"}


TOKEN = "sx_synthetic_only_token_value_0001"


def test_sparkx_console_token_is_write_only_and_takes_precedence(owner, tmp_path):
    client, _, settings = owner
    settings.sparkx_credentials_file = str(tmp_path / "secrets" / "sparkx.json")
    settings.sparkx_ai_token = SecretStr("sx_environment_token_value_0002")
    settings.sparkx_profile_ids = "111"
    headers = _headers(client)
    before = client.get("/console/api/sparkx").json()
    assert before["token_source"] == "environment" and before["profile_ids"] == "111"
    bad = client.post("/console/api/sparkx", json={"revision": before["revision"], "token": TOKEN,
                                                   "profile_ids": "12a"}, headers=headers)
    assert bad.status_code == 400 and TOKEN not in bad.text
    saved = client.post("/console/api/sparkx", json={"revision": before["revision"], "token": TOKEN,
                                                     "profile_ids": " 222, 333,222 "}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert TOKEN not in saved.text and saved.json()["token_source"] == "console"
    assert saved.json()["profile_ids"] == "222,333"
    path = Path(settings.sparkx_credentials_file)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    effective = sparkx_config.with_console_credentials(settings)
    assert effective.sparkx_ai_token.get_secret_value() == TOKEN and effective.sparkx_profile_ids == "222,333"
    # Blank token keeps the saved one; only the allow-list changes.
    kept = client.post("/console/api/sparkx", json={"revision": saved.json()["revision"], "token": "",
                                                    "profile_ids": "444"}, headers=headers)
    assert kept.status_code == 200
    assert sparkx_config.with_console_credentials(settings).sparkx_ai_token.get_secret_value() == TOKEN
    stale = client.post("/console/api/sparkx", json={"revision": saved.json()["revision"], "token": "",
                                                     "profile_ids": "555"}, headers=headers)
    assert stale.status_code == 409
    # An unsafe file disables SparkX instead of silently using the environment token.
    path.write_text("{not json", encoding="utf-8")
    assert sparkx_config.with_console_credentials(settings).sparkx_ai_token.get_secret_value() == ""


def _authorized(profiles):
    value = {"isError": False, "toolName": "get_user_authorized_context", "userId": "u-1", "tenantId": "t-1",
             "profiles": profiles}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value))], structuredContent=value)


def test_sparkx_connection_check_lists_authorized_profiles_and_classifies_failure(tmp_path, monkeypatch):
    from jx_alert.config import Settings

    settings = Settings(_env_file=None, sparkx_credentials_file=str(tmp_path / "sparkx.json"),
                        sparkx_ai_token=SecretStr(TOKEN), sparkx_profile_ids="1001,9999")
    calls = []

    async def authorized(tool, arguments, **kwargs):
        calls.append(tool)
        assert kwargs["token"] == TOKEN and arguments == {}
        return _authorized([{"profileId": 1001, "profileName": "Store-US", "countryCode": "US"},
                            {"profileId": "1002", "profileName": "Store-CA", "countryCode": "CA"}])

    monkeypatch.setattr(sparkx_direct, "call_sparkx", authorized)
    result = sparkx_config.check_connection(settings)
    assert calls == ["get_user_authorized_context"]
    assert result["status"] == "OK" and result["profile_count"] == 2
    assert result["profiles"][0] == {"profile_id": "1001", "name": "Store-US", "country": "US", "allowed": True}
    assert result["allowed_not_authorized"] == ["9999"]
    assert "u-1" not in json.dumps(result) and "t-1" not in json.dumps(result) and TOKEN not in json.dumps(result)

    async def rejected(tool, arguments, **kwargs):
        raise sparkx_direct.SparkXDirectError("rejected", "AUTH_REJECTED")

    monkeypatch.setattr(sparkx_direct, "call_sparkx", rejected)
    assert sparkx_config.check_connection(settings)["status"] == "AUTH_REJECTED"


@pytest.mark.parametrize("response,code", [
    (lambda request: httpx.Response(401, json={"error": "invalid_token"}), "AUTH_REJECTED"),
    (lambda request: httpx.Response(403, json={}), "FORBIDDEN"),
    (lambda request: httpx.Response(503, json={}), "PROVIDER_ERROR"),
])
def test_sparkx_transport_failure_is_classified_without_provider_text(response, code):
    import anyio

    async def call():
        await sparkx_direct.call_sparkx("get_user_authorized_context", {}, token=TOKEN, timeout_seconds=5,
                                        transport=httpx.MockTransport(response))

    with pytest.raises(sparkx_direct.SparkXDirectError) as caught:
        anyio.run(call)
    assert caught.value.code == code
    assert "invalid_token" not in str(caught.value) and TOKEN not in str(caught.value)


def test_sparkx_report_console_settings_mapping_and_preview(owner, tmp_path, monkeypatch):
    from jx_alert import sparkx_reports

    client, _, settings = owner
    settings.sparkx_report_settings_file = str(tmp_path / "sparkx-report.json")
    settings.sparkx_credentials_file = str(tmp_path / "sparkx.json")
    stores = [{"sid": "5174", "name": "川页-US", "country": "US", "operator": "皮泽霖"},
              {"sid": "5175", "name": "汤宝-US", "country": "US", "operator": "未映射运营"}]
    monkeypatch.setattr(sparkx_reports, "jx_stores", lambda db, s: (stores, {}))
    assert client.get("/console/api/sparkx-report").status_code == 401
    headers = _headers(client)
    view = client.get("/console/api/sparkx-report").json()
    assert view["enabled"] is False and view["sparkx_configured"] is False and view["stores"] == stores
    missing = client.post("/console/api/sparkx-report/profiles", json={}, headers=headers)
    assert missing.status_code == 409 and "尚未配置 SparkX MCP Token" in missing.json()["error"]
    monkeypatch.setattr(sparkx_reports, "list_profiles", lambda s: (
        {"101": {"name": "香港川页", "country": "US", "store_type": None},
         "102": {"name": "OCTORA-主账号", "country": "US", "store_type": None}}, ["101"]))
    profiles = client.post("/console/api/sparkx-report/profiles", json={}, headers=headers).json()["profiles"]
    assert {row["profile_id"]: row["suggested"] for row in profiles} == {"101": "5174", "102": None}
    assert {row["profile_id"]: row["allowed"] for row in profiles} == {"101": True, "102": False}
    body = {"revision": view["revision"], "enabled": True, "daily_source": "sparkx",
            "destinations": sparkx_reports.DESTINATIONS, "routes": {"101": "5174", "102": ""}}
    assert client.post("/console/api/sparkx-report", json=body, headers={"Origin": ORIGIN}).status_code == 403
    saved = client.post("/console/api/sparkx-report", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["routes"] == {"101": "5174"} and saved.json()["enabled"] is True
    assert client.post("/console/api/sparkx-report", json=body, headers=headers).status_code == 409
    duplicated = ('{"revision":"' + saved.json()["revision"] + '","enabled":true,"enabled":false,'
                  '"daily_source":"sparkx","destinations":{},"routes":{}}')
    assert client.post("/console/api/sparkx-report", content=duplicated,
                       headers={**headers, "Content-Type": "application/json"}).status_code == 400
    card = {"schema": "2.0", "header": {"title": {"content": "SparkX 广告日报"}}, "body": {"elements": []}}
    monkeypatch.setattr(sparkx_reports, "preview", lambda s, kind, now: (card, {
        "report_date": "2026-11-22", "window": ["2026-11-16", "2026-11-22"],
        "previous_window": ["2026-11-09", "2026-11-15"], "profiles": {"101": {}}, "errors": {}}))
    result = client.post("/console/api/sparkx-report/preview", json={"kind": "daily"}, headers=headers)
    assert result.status_code == 200 and result.json()["card"] == card and result.json()["profiles"] == 1
    assert client.post("/console/api/sparkx-report/preview", json={"kind": "weekly"},
                       headers=headers).status_code == 400


def test_backend_error_code_survives_base_class_initialisation():
    from jx_alert.sparkx_backend import SparkXBackendError

    assert SparkXBackendError("SPARKX_EMPTY_NOT_ZERO").code == "SPARKX_EMPTY_NOT_ZERO"
    assert sparkx_direct.SparkXDirectError("plain").code is None
