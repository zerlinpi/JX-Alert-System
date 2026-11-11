import json
import os
from types import SimpleNamespace

import pytest

from jx_alert.lingxing.client import LingxingError
from jx_alert.lingxing.token import LingxingTokenProvider, request_token


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, calls, response, **kwargs):
        self.calls = calls
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _install_fake_client(monkeypatch, calls, response):
    def factory(*args, **kwargs):
        return FakeClient(calls, response, **kwargs)

    monkeypatch.setattr("jx_alert.lingxing.token.httpx.Client", factory)


def test_request_token_multipart_uses_regular_form_fields(monkeypatch):
    calls = []
    _install_fake_client(
        monkeypatch,
        calls,
        FakeResponse(
            {
                "code": "200",
                "msg": "OK",
                "data": {
                    "access_token": "access-value",
                    "refresh_token": "refresh-value",
                    "expires_in": 7199,
                },
            }
        ),
    )

    result = request_token(
        base_url="https://openapi.lingxing.com",
        path="/api/auth-server/oauth/access-token",
        request_json='{"appId":"app-123","appSecret":"secret-456"}',
        request_format="multipart",
    )

    assert result["code"] == "200"
    assert calls[0][0] == (
        "https://openapi.lingxing.com/api/auth-server/oauth/access-token"
    )
    kwargs = calls[0][1]
    assert kwargs["files"] == {
        "appId": (None, "app-123"),
        "appSecret": (None, "secret-456"),
    }
    assert "json" not in kwargs
    assert "data" not in kwargs
    assert "headers" not in kwargs


def test_request_token_json_format_remains_backward_compatible(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, calls, FakeResponse({"code": "200", "data": {}}))

    result = request_token(
        base_url="https://openapi.lingxing.com",
        path="/token",
        request_json='{"client":"app"}',
        request_format="json",
    )

    assert result["code"] == "200"
    assert calls[0][1]["json"] == {"client": "app"}


def test_multipart_rejects_nested_values_before_network_call(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, calls, FakeResponse({"code": "200", "data": {}}))

    with pytest.raises(LingxingError, match="scalar values"):
        request_token(
            base_url="https://openapi.lingxing.com",
            path="/token",
            request_json='{"appId":"app-123","options":{"nested":true}}',
            request_format="multipart",
        )

    assert calls == []


def test_provider_loads_nested_token_response_and_persists_ttl(
    monkeypatch, tmp_path
):
    calls = []
    _install_fake_client(
        monkeypatch,
        calls,
        FakeResponse(
            {
                "code": "200",
                "msg": "OK",
                "data": {
                    "access_token": "access-secret-value",
                    "refresh_token": "refresh-secret-value",
                    "expires_in": 7199,
                },
            }
        ),
    )
    token_path = tmp_path / "secrets" / "lingxing-token.json"
    settings = SimpleNamespace(
        lingxing_token_store_path=str(token_path),
        lingxing_access_token="",
        lingxing_app_id="app-123",
        lingxing_app_secret="app-secret-value",
        lingxing_refresh_token="",
        lingxing_token_path="/api/auth-server/oauth/access-token",
        lingxing_token_method="POST",
        lingxing_token_request_format="multipart",
        lingxing_token_request_json=(
            '{"appId":"${LINGXING_APP_ID}",'
            '"appSecret":"${LINGXING_APP_SECRET}"}'
        ),
        lingxing_token_response_path="data.access_token",
        lingxing_refresh_response_path="data.refresh_token",
        lingxing_expires_in_response_path="data.expires_in",
        lingxing_refresh_path="",
        lingxing_refresh_method="POST",
        lingxing_refresh_request_format="json",
        lingxing_refresh_request_json="{}",
        lingxing_base_url="https://openapi.lingxing.com",
        lingxing_timeout=30,
    )

    before = int(__import__("time").time())
    access_token = LingxingTokenProvider(settings).get_access_token()
    after = int(__import__("time").time())

    assert access_token == "access-secret-value"
    kwargs = calls[0][1]
    assert kwargs["files"] == {
        "appId": (None, "app-123"),
        "appSecret": (None, "app-secret-value"),
    }
    assert "json" not in kwargs
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "access-secret-value"
    assert saved["refresh_token"] == "refresh-secret-value"
    assert before + 7199 <= saved["expires_at"] <= after + 7199
    assert os.stat(token_path).st_mode & 0o777 == 0o600
