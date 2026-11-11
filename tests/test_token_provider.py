import json
import time
from types import SimpleNamespace

from jx_alert.lingxing.token import LingxingTokenProvider
from jx_alert.lingxing.client import LingxingError


def _settings(token_path):
    return SimpleNamespace(
        lingxing_access_token="",
        lingxing_app_id="app-id",
        lingxing_app_secret="app-secret",
        lingxing_refresh_token="",
        lingxing_token_path="/token",
        lingxing_refresh_path="/refresh",
        lingxing_token_request_json="{}",
        lingxing_refresh_request_json="{}",
        lingxing_token_response_path="data.access_token",
        lingxing_refresh_response_path="data.refresh_token",
        lingxing_expires_in_response_path="data.expires_in",
        lingxing_token_method="POST",
        lingxing_refresh_method="POST",
        lingxing_token_store_path=str(token_path),
        lingxing_timeout=10,
        lingxing_base_url="https://openapi.lingxing.com",
    )


def test_refresh_token_is_rotated_before_two_hour_expiry(tmp_path, monkeypatch):
    token_file = tmp_path / "secrets" / "token.json"
    now = int(time.time())
    token_file.parent.mkdir()
    token_file.write_text(json.dumps({
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": now + 3600,
        "refresh_expires_at": now + 60,
    }), encoding="utf-8")
    provider = LingxingTokenProvider(_settings(token_file))
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs["path"])
        return {"data": {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        }}

    monkeypatch.setattr(provider, "_call", fake_call)
    assert provider.get_access_token() == "new-access"
    assert calls == ["/refresh"]
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "new-refresh"
    assert saved["refresh_expires_at"] >= now + 7000
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_expired_refresh_token_falls_back_to_app_credentials(tmp_path, monkeypatch):
    token_file = tmp_path / "token.json"
    now = int(time.time())
    token_file.write_text(json.dumps({
        "access_token": "old-access",
        "refresh_token": "expired-refresh",
        "expires_at": now - 10,
        "refresh_expires_at": now - 100,
    }), encoding="utf-8")
    provider = LingxingTokenProvider(_settings(token_file))
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs["path"])
        if kwargs["path"] == "/refresh":
            raise LingxingError("expired refresh token")
        return {"data": {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "expires_in": 7200,
        }}

    monkeypatch.setattr(provider, "_call", fake_call)
    assert provider.get_access_token() == "fresh-access"
    assert calls == ["/refresh", "/token"]
