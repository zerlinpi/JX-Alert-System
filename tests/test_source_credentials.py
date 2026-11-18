"""Connections rotate privately and readers never combine old/new identities."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import ModuleType
import sys

import pytest

from jx_alert.config import Settings
from jx_alert.lingxing import source_credentials as sources


CLOCK = datetime(2026, 11, 17, 7, tzinfo=timezone.utc)
MCP_KEY = "synthetic-mcp-private-key-123456789"
API_SECRET = "synthetic-api-private-secret"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, lingxing_sources_file=str(tmp_path / "private" / "sources.json"),
        lingxing_business_credentials_file=str(tmp_path / "legacy.json"),
        lingxing_token_store_path=str(tmp_path / "old-token.json"))


def save(settings, kind="mcp", credentials=None, **kwargs):
    credentials = credentials or {"url": sources.MCP_URL, "key": MCP_KEY}
    return sources.save_source(settings, kind=kind, credentials=credentials,
        revision=sources.source_configuration(settings, now=CLOCK)["revision"], now=CLOCK, **kwargs)


def test_add_and_rotate_are_private_with_metadata_only_audit(settings):
    first = save(settings, expires_on="2026-12-16")
    assert first["mcp"]["configured"] and first["mcp"]["expires_on"] == "2026-12-16"
    assert first["events"] == [{"at": CLOCK.isoformat(), "kind": "mcp", "action": "ADD", "expires_on": "2026-12-16"}]
    assert first["mcp"]["origin"] == sources.MCP_ORIGIN
    text = json.dumps(first)
    assert all(key not in text for key in (MCP_KEY, '"credentials"', '"version_id"', '"key"'))
    path = Path(settings.lingxing_sources_file)
    assert sources.mcp_credentials(settings, now=CLOCK) == (sources.MCP_URL, MCP_KEY)
    old = first["revision"]
    second = save(settings, credentials={"key": MCP_KEY + "-new"}, expires_on="2027-01-16")
    assert second["revision"] != old and second["events"][0]["action"] == "ROTATE"
    assert sources.mcp_credentials(settings, now=CLOCK)[1] == MCP_KEY + "-new"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_wrong_revision_never_overwrites_credentials_or_audit(settings):
    save(settings)
    path = Path(settings.lingxing_sources_file)
    before = path.read_bytes()
    with pytest.raises(sources.SourceCredentialsError) as caught:
        sources.save_source(settings, revision="0" * 64, kind="mcp", credentials={"key": "replacement-secret-key"})
    assert caught.value.status_code == 409 and path.read_bytes() == before


@pytest.mark.parametrize("url", [
    "http://openmcp.lingxing.com/mcp-servers/lingxing-mcp",
    "https://localhost/mcp", "https://127.0.0.1/mcp", "https://169.254.169.254/mcp",
    "https://openmcp.lingxing.com.evil.example/mcp-servers/lingxing-mcp",
    sources.MCP_URL + "?key=do-not-include-keys", sources.MCP_URL + "#fragment",
    "https://owner:secret@openmcp.lingxing.com/mcp-servers/lingxing-mcp",
])
def test_mcp_target_is_the_exact_official_endpoint(settings, url):
    with pytest.raises(sources.SourceCredentialsError) as caught:
        save(settings, credentials={"url": url, "key": MCP_KEY})
    assert caught.value.status_code == 400 and MCP_KEY not in str(caught.value)
    assert not Path(settings.lingxing_sources_file).exists()


@pytest.mark.parametrize("kind,credentials", [
    ("other", {"key": MCP_KEY}), ([], {"key": MCP_KEY}),
    ("mcp", {"url": sources.MCP_URL, "key": "short"}),
    ("mcp", {"url": sources.MCP_URL, "key": MCP_KEY + "\nsecret"}),
    ("mcp", {"url": sources.MCP_URL, "key": MCP_KEY + "\x00secret"}),
    ("mcp", {"url": sources.MCP_URL, "key": MCP_KEY, "path": "/tmp/evil"}),
    ("api", {"app_id": "id"}), ("api", {"app_id": "id", "app_secret": API_SECRET, "base_url": "https://evil"}),
])
def test_unsupported_or_invalid_credentials_cannot_be_written(settings, kind, credentials):
    with pytest.raises(sources.SourceCredentialsError):
        sources.save_source(settings, kind=kind, credentials=credentials, revision="0" * 64, now=CLOCK)
    assert not Path(settings.lingxing_sources_file).exists()


def test_expiry_is_the_end_of_the_declared_pacific_date_and_never_reverts_to_legacy(settings):
    settings.lingxing_mcp_url = sources.MCP_URL
    settings.lingxing_mcp_key = "legacy-secret-key-123456789"
    save(settings, expires_on="2026-11-16")  # 07:00 UTC is still Nov 16 in Pacific time.
    assert sources.mcp_credentials(settings, now=CLOCK)[1] == MCP_KEY
    next_day = CLOCK.replace(hour=8)
    assert sources.source_configuration(settings, now=next_day)["mcp"]["status"] == "EXPIRED"
    with pytest.raises(sources.SourceCredentialsError, match="更换日期"):
        sources.mcp_credentials(settings, now=next_day)


def test_api_rotation_uses_a_new_cache_and_a_single_snapshot_without_touching_env(settings, tmp_path):
    original_env = tmp_path / ".env"
    original_env.write_text("private content untouched", encoding="utf-8")
    old_cache = Path(settings.lingxing_token_store_path)
    old_cache.write_text('{"access_token":"old-cached-token"}', encoding="utf-8")
    assert sources.effective_api_settings(settings) is settings
    save(settings, "api", {"app_id": "application-one", "app_secret": API_SECRET})
    one = sources.effective_api_settings(settings, now=CLOCK)
    assert one.lingxing_app_id == "application-one" and one.lingxing_app_secret == API_SECRET
    assert one.lingxing_token_store_path != settings.lingxing_token_store_path
    assert sources.effective_api_settings(one, now=CLOCK).lingxing_token_store_path == one.lingxing_token_store_path
    save(settings, "api", {"app_id": "application-two", "app_secret": API_SECRET + "-new"})
    two = sources.effective_api_settings(settings, now=CLOCK)
    assert two.lingxing_token_store_path != one.lingxing_token_store_path
    assert one.lingxing_app_id == "application-one" and two.lingxing_app_id == "application-two"
    assert two.lingxing_app_key == "application-two"
    assert old_cache.read_text() == '{"access_token":"old-cached-token"}'
    assert original_env.read_text() == "private content untouched"
    assert API_SECRET not in json.dumps(sources.source_configuration(settings))


def test_expired_api_does_not_fall_back_to_environment(settings):
    settings.lingxing_access_token = "legacy-token"
    settings.lingxing_app_id = "legacy-app"
    save(settings, "api", {"app_id": "new-app", "app_secret": API_SECRET}, expires_on="2026-11-16")
    with pytest.raises(sources.SourceCredentialsError):
        sources.effective_api_settings(settings, now=CLOCK.replace(hour=8))


def test_mcp_rotation_and_expiry_do_not_invalidate_the_independent_api_cache(settings):
    save(settings, "api", {"app_id": "app-one", "app_secret": API_SECRET})
    before = sources.effective_api_settings(settings, now=CLOCK)
    save(settings, expires_on="2026-11-16")
    after = sources.effective_api_settings(settings, now=CLOCK.replace(hour=8))
    assert before.lingxing_token_store_path == after.lingxing_token_store_path
    with pytest.raises(sources.SourceCredentialsError):
        sources.mcp_credentials(settings, now=CLOCK.replace(hour=8))


@pytest.mark.parametrize("expiry", ["2026-11-01", "2026-2-3", "not-a-date", 20261216])
def test_replacement_dates_must_be_valid_and_not_before_current_pacific_day(settings, expiry):
    with pytest.raises(sources.SourceCredentialsError):
        save(settings, expires_on=expiry)
    assert not Path(settings.lingxing_sources_file).exists()


def test_explicit_secret_update_clears_old_static_token_and_blanks_preserve_current_values(settings):
    save(settings, "api", {"app_id": "app", "access_token": "synthetic-static-token"})
    save(settings, "api", {"app_secret": API_SECRET, "access_token": ""})
    managed = sources.effective_api_settings(settings, now=CLOCK)
    assert managed.lingxing_access_token == "" and managed.lingxing_app_secret == API_SECRET
    save(settings, "api", {"app_secret": "", "app_id": ""}, expires_on="2026-12-16")
    assert sources.effective_api_settings(settings, now=CLOCK).lingxing_app_secret == API_SECRET


def test_new_app_identity_requires_explicit_matching_authentication(settings):
    save(settings, "api", {"app_id": "first-app", "app_secret": API_SECRET,
                          "access_token": "token-for-first-app"})
    path = Path(settings.lingxing_sources_file)
    before = path.read_bytes()
    with pytest.raises(sources.SourceCredentialsError, match="更换App ID"):
        save(settings, "api", {"app_id": "second-app", "app_secret": "", "access_token": ""})
    assert path.read_bytes() == before
    save(settings, "api", {"app_id": "second-app", "access_token": "token-for-second-app"})
    effective = sources.effective_api_settings(settings, now=CLOCK)
    assert effective.lingxing_app_id == "second-app" and effective.lingxing_access_token == "token-for-second-app"
    assert effective.lingxing_app_secret == "" and effective.lingxing_app_key == "second-app"


def test_new_app_identity_does_not_keep_old_static_token_when_new_secret_is_provided(settings):
    save(settings, "api", {"app_id": "first-app", "access_token": "token-for-first-app"})
    save(settings, "api", {"app_id": "second-app", "app_secret": "secret-for-second-app"})
    effective = sources.effective_api_settings(settings, now=CLOCK)
    assert effective.lingxing_app_id == "second-app" and effective.lingxing_access_token == ""
    assert effective.lingxing_app_secret == "secret-for-second-app"


def test_failed_atomic_replace_preserves_previous_credentials_and_events(settings, monkeypatch):
    save(settings)
    path = Path(settings.lingxing_sources_file)
    original = path.read_bytes()
    monkeypatch.setattr(sources.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("private internal failure")))
    with pytest.raises(OSError):
        save(settings, credentials={"key": "new-private-key-123456"})
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".lingxing-sources-*"))


def test_bad_legacy_mcp_can_be_replaced_without_disclosing_it(settings):
    path = Path(settings.lingxing_business_credentials_file)
    path.write_text('{"url":"https://not-official.example/mcp","key":"old-private-key-123456"}', encoding="utf-8")
    os.chmod(path, 0o600)
    before = path.read_bytes()
    assert sources.source_configuration(settings)["mcp"]["status"] == "INVALID"
    save(settings)
    assert sources.mcp_credentials(settings, now=CLOCK)[1] == MCP_KEY
    assert path.read_bytes() == before


def test_legacy_file_remains_readable_without_migration(settings):
    path = Path(settings.lingxing_business_credentials_file)
    path.write_text(json.dumps({"url": sources.MCP_URL, "key": MCP_KEY}), encoding="utf-8")
    os.chmod(path, 0o600)
    assert sources.mcp_credentials(settings) == (sources.MCP_URL, MCP_KEY)
    assert sources.source_configuration(settings)["mcp"]["updated_at"] is None
    assert not Path(settings.lingxing_sources_file).exists()


def test_client_cannot_mix_token_and_signing_identity_during_rotation(settings, monkeypatch):
    from jx_alert.lingxing.client import LingxingClient
    from jx_alert import config
    save(settings, "api", {"app_id": "app-one", "app_secret": API_SECRET})

    class Provider:
        def __init__(self, original):
            self.settings = sources.effective_api_settings(original)

        def get_access_token(self):
            save(settings, "api", {"app_id": "app-two", "app_secret": API_SECRET + "-new"})
            return "token-for-app-one"

    module = ModuleType("jx_alert.lingxing.token")
    module.LingxingTokenProvider = Provider
    monkeypatch.setitem(sys.modules, "jx_alert.lingxing.token", module)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    client = LingxingClient.from_settings()
    assert client.app_id == "app-one" and client.access_token == "token-for-app-one"


def test_legacy_api_contract_remains_unchanged_without_a_private_api_entry(settings):
    settings.lingxing_token_path = "/legacy-token"
    settings.lingxing_token_request_json = '{"old":"literal-legacy-value"}'
    settings.lingxing_refresh_path = "/legacy-refresh"
    before = settings.model_dump()
    # An independent MCP configuration cannot change the legacy API contract.
    save(settings)
    assert sources.effective_api_settings(settings, now=CLOCK) is settings
    assert settings.model_dump() == before


def test_private_api_replaces_literal_legacy_oauth_templates_without_mutating_settings(settings):
    settings.lingxing_token_path = "/legacy-token"
    settings.lingxing_token_method = "GET"
    settings.lingxing_token_request_format = "json"
    settings.lingxing_token_request_json = '{"appId":"old-id","appSecret":"old-secret"}'
    settings.lingxing_refresh_path = "/legacy-refresh"
    settings.lingxing_refresh_method = "GET"
    settings.lingxing_refresh_request_format = "json"
    settings.lingxing_refresh_request_json = '{"appId":"old-id","refreshToken":"old-refresh"}'
    settings.lingxing_token_response_path = "legacy.access"
    settings.lingxing_refresh_response_path = "legacy.refresh"
    settings.lingxing_expires_in_response_path = "legacy.ttl"
    before = settings.model_dump()
    save(settings, "api", {"app_id": "new-id", "app_secret": API_SECRET, "app_key": "custom-signing-key"})
    active = sources.effective_api_settings(settings, now=CLOCK)
    assert active.lingxing_token_path == "/api/auth-server/oauth/access-token"
    assert active.lingxing_refresh_path == "/api/auth-server/oauth/refresh"
    assert active.lingxing_token_method == active.lingxing_refresh_method == "POST"
    assert active.lingxing_token_request_format == active.lingxing_refresh_request_format == "multipart"
    assert json.loads(active.lingxing_token_request_json) == {
        "appId": "${LINGXING_APP_ID}", "appSecret": "${LINGXING_APP_SECRET}"}
    assert json.loads(active.lingxing_refresh_request_json) == {
        "appId": "${LINGXING_APP_ID}", "refreshToken": "${LINGXING_REFRESH_TOKEN}"}
    assert (active.lingxing_token_response_path, active.lingxing_refresh_response_path,
        active.lingxing_expires_in_response_path) == ("data.access_token", "data.refresh_token", "data.expires_in")
    assert active.lingxing_app_key == "custom-signing-key"
    assert settings.model_dump() == before


@pytest.mark.parametrize("app_id", ["aaaaaaaaaaaaaaaa", "0123456789abcdef"])
def test_rotated_private_credentials_reach_actual_multipart_token_and_refresh_requests(
        settings, monkeypatch, app_id):
    pytest.importorskip("fcntl", reason="The production token cache uses Linux file locks")
    import httpx
    from jx_alert import config
    from jx_alert.lingxing import token
    from jx_alert.lingxing.client import LingxingClient

    # Reproduce the real regression: the old identity is hardcoded in the
    # request templates, including when only the secret (not App ID) changes.
    settings.lingxing_app_id = "aaaaaaaaaaaaaaaa"
    settings.lingxing_app_secret = "old-synthetic-secret"
    settings.lingxing_access_token = "old-static-token"
    settings.lingxing_refresh_token = "old-static-refresh"
    settings.lingxing_token_path = "/legacy-token"
    settings.lingxing_token_request_json = '{"appId":"aaaaaaaaaaaaaaaa","appSecret":"old-synthetic-secret"}'
    settings.lingxing_refresh_request_json = '{"appId":"aaaaaaaaaaaaaaaa","refreshToken":"old-static-refresh"}'
    old_cache = Path(settings.lingxing_token_store_path)
    old_cache.write_text('{"access_token":"unrelated-legacy-cache"}', encoding="utf-8")
    original = old_cache.read_bytes()
    new_secret = 'new-synthetic-secret-with-"-\\-$-&-+-/'
    save(settings, "api", {"app_id": app_id, "app_secret": new_secret, "app_key": "private-signing-key"})
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    clock = 1_795_000_000
    monkeypatch.setattr(token.time, "time", lambda: clock)
    calls = []

    def receive(request):
        body = request.read()
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert (f'name="appId"\r\n\r\n{app_id}\r\n').encode() in body
        assert b"old-synthetic-secret" not in body and b"old-static-refresh" not in body
        if not calls:
            assert str(request.url) == sources.API_ORIGIN + "/api/auth-server/oauth/access-token"
            assert (f'name="appSecret"\r\n\r\n{new_secret}\r\n').encode() in body
        else:
            assert str(request.url) == sources.API_ORIGIN + "/api/auth-server/oauth/refresh"
            assert b'name="refreshToken"\r\n\r\nrefresh-1\r\n' in body
            assert b'name="appSecret"' not in body
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 200, "data": {
            "access_token": f"access-{len(calls)}", "refresh_token": f"refresh-{len(calls)}", "expires_in": 7200}})

    real_client = httpx.Client
    monkeypatch.setattr(token.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(receive), trust_env=False, **kwargs))
    first = LingxingClient.from_settings()
    assert first.app_id == app_id and first.app_key == "private-signing-key"
    assert first.access_token == "access-1"
    clock += 6301  # The still-valid access token is due for single-use refresh.
    second = LingxingClient.from_settings()
    assert second.app_id == app_id and second.access_token == "access-2"
    assert len(calls) == 2
    assert old_cache.read_bytes() == original
    current_cache = json.loads(Path(second._collection_settings.lingxing_token_store_path).read_text())
    assert current_cache["refresh_token"] == "refresh-2"


def test_source_file_symlink_is_not_followed(settings, tmp_path):
    path = Path(settings.lingxing_sources_file)
    path.parent.mkdir()
    target = tmp_path / "unrelated.json"
    target.write_text("unrelated content")
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symbolic links unavailable for current OS user")
    with pytest.raises(sources.SourceCredentialsError):
        sources.source_configuration(settings)
    assert target.read_text() == "unrelated content"
