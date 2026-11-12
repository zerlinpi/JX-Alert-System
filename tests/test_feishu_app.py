import json

import httpx
import pytest
from types import SimpleNamespace

from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs,
        ),
    )


def token_response(value="t-test-token", expire=7200):
    return httpx.Response(200, json={
        "code": 0, "tenant_access_token": value, "expire": expire,
    })


def message_response():
    return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_message"}})


def send(client, key="test-key", **kwargs):
    return client.send_text(
        "订单异常", receive_id="oc_operations", idempotency_key=key, **kwargs,
    )


def test_group_mentions_and_token_cache(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/internal"):
            assert json.loads(request.content) == {"app_id": "cli_test", "app_secret": "secret"}
            return token_response()
        assert request.headers["Authorization"] == "Bearer t-test-token"
        assert request.url.params["receive_id_type"] == "chat_id"
        return message_response()

    mock_http(monkeypatch, handler)
    client = FeishuAppClient("cli_test", "secret")
    result = send(client, open_ids=["ou_operator", "ou_admin", "ou_operator"])
    send(client, key="another")
    assert result == {"message_id": "om_message"}
    assert len(requests) == 3
    payload = json.loads(requests[1].content)
    assert payload["receive_id"] == "oc_operations"
    assert payload["msg_type"] == "text"
    text = json.loads(payload["content"])["text"]
    assert text == '<at user_id="ou_operator">提醒</at> <at user_id="ou_admin">提醒</at> 订单异常'
    assert len(payload["uuid"]) <= 50
    assert payload["uuid"] != json.loads(requests[2].content)["uuid"]


def test_direct_manager_message_and_injected_at_is_escaped(monkeypatch):
    messages = []

    def handler(request):
        if request.url.path.endswith("/internal"):
            return token_response()
        assert request.url.params["receive_id_type"] == "open_id"
        messages.append(json.loads(request.content))
        return message_response()

    mock_http(monkeypatch, handler)
    client = FeishuAppClient("cli_test", "secret")
    client.send_text(
        '<at user_id="all">everyone</at> & SKU <test>',
        receive_id="ou_manager", receive_id_type="open_id", idempotency_key="manager-key",
    )
    assert messages[0]["receive_id"] == "ou_manager"
    text = json.loads(messages[0]["content"])["text"]
    assert '<at user_id="all">' not in text
    assert '&lt;at user_id="all"&gt;' in text
    assert "&amp; SKU &lt;test&gt;" in text


@pytest.mark.parametrize("http_status", [200, 400, 401])
def test_expired_token_refreshes_once_and_reuses_uuid(monkeypatch, http_status):
    tokens, messages = [], []

    def handler(request):
        if request.url.path.endswith("/internal"):
            value = f"t-token-{len(tokens)}"
            tokens.append(value)
            return token_response(value)
        messages.append(request)
        if len(messages) == 1:
            return httpx.Response(http_status, json={"code": 99991663, "msg": "expired"})
        return message_response()

    mock_http(monkeypatch, handler)
    send(FeishuAppClient("cli_test", "secret"))
    assert len(tokens) == 2
    assert len(messages) == 2
    assert messages[0].headers["Authorization"] != messages[1].headers["Authorization"]
    assert json.loads(messages[0].content)["uuid"] == json.loads(messages[1].content)["uuid"]


def test_expired_token_cannot_loop_indefinitely(monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/internal"):
            return token_response()
        return httpx.Response(200, json={"code": 99991663})

    mock_http(monkeypatch, handler)
    with pytest.raises(FeishuAppError, match="code=99991663"):
        send(FeishuAppClient("cli_test", "secret"))
    assert len(paths) == 4


def test_token_refresh_uses_actual_expiry_with_margin(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("jx_alert.notifications.feishu_app.time.monotonic", lambda: clock[0])
    auth_count = []

    def handler(request):
        if request.url.path.endswith("/internal"):
            auth_count.append(request)
            return token_response(expire=100)
        return message_response()

    mock_http(monkeypatch, handler)
    client = FeishuAppClient("cli_test", "secret")
    send(client)
    clock[0] = 189.0
    send(client)
    assert len(auth_count) == 1
    clock[0] = 190.0
    send(client)
    assert len(auth_count) == 2


def test_durable_retry_and_new_client_reuse_same_uuid(monkeypatch):
    uuids = []

    def handler(request):
        if request.url.path.endswith("/internal"):
            return token_response()
        uuids.append(json.loads(request.content)["uuid"])
        return message_response()

    mock_http(monkeypatch, handler)
    send(FeishuAppClient("cli_test", "secret"), key="same durable outbox key" * 20)
    send(FeishuAppClient("cli_test", "secret"), key="same durable outbox key" * 20)
    assert uuids[0] == uuids[1]
    assert len(uuids[0]) <= 50


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"code": 230013, "msg": "secret t-test-token"}),
    httpx.Response(403, json={"code": 230027, "msg": "secret t-test-token"}),
    httpx.Response(200, json={"code": 0, "data": {}}),
    httpx.Response(200, json={"code": 0, "data": []}),
    httpx.Response(200, json={"msg": "secret t-test-token"}),
    httpx.Response(200, json={"code": False}),
    httpx.Response(200, json=[]),
    httpx.Response(200, text="secret t-test-token"),
])
def test_rejected_and_malformed_message_responses_are_sanitized(monkeypatch, response):
    def handler(request):
        if request.url.path.endswith("/internal"):
            return token_response()
        return response

    mock_http(monkeypatch, handler)
    with pytest.raises(FeishuAppError) as error:
        send(FeishuAppClient("cli_test", "secret"))
    assert "secret" not in str(error.value)
    assert "t-test-token" not in str(error.value)


@pytest.mark.parametrize("response", [
    {"code": 10003, "msg": "secret"},
    {"code": 0, "tenant_access_token": "t-test-token"},
    {"code": 0, "tenant_access_token": "", "expire": 7200},
    {"code": 0, "tenant_access_token": "t-test-token", "expire": 0},
    {"code": 0, "tenant_access_token": "t-test-token", "expire": True},
])
def test_invalid_token_response_is_rejected_without_sending(monkeypatch, response):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    mock_http(monkeypatch, handler)
    with pytest.raises(FeishuAppError) as error:
        send(FeishuAppClient("cli_test", "secret"))
    assert len(requests) == 1
    assert "secret" not in str(error.value)
    assert "t-test-token" not in str(error.value)


def test_http_errors_do_not_expose_credentials_or_url(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("secret t-test-token https://example.test", request=request)

    mock_http(monkeypatch, handler)
    with pytest.raises(FeishuAppError) as error:
        send(FeishuAppClient("cli_test", "secret"))
    assert str(error.value) == "Feishu app HTTP request failed: ConnectError"
    assert error.value.__suppress_context__


@pytest.mark.parametrize("kwargs", [
    {"base_url": "https://example.com"},
    {"base_url": "https://open.feishu.cn@evil.test"},
    {"base_url": "https://open.feishu.cn/?token=secret"},
    {"base_url": "http://open.feishu.cn"},
    {"timeout": 0},
])
def test_client_rejects_unsafe_configuration(kwargs):
    with pytest.raises(ValueError):
        FeishuAppClient("cli_test", "secret", **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"receive_id": "all"},
    {"receive_id": "ou_manager", "receive_id_type": "chat_id"},
    {"receive_id": "oc_ops", "receive_id_type": "email"},
    {"receive_id": "oc_ops", "open_ids": ["all"]},
    {"receive_id": "oc_ops", "open_ids": ['ou_a\"><at user_id="all']},
    {"receive_id": "oc_ops", "open_ids": "ou_operator"},
    {"receive_id": "oc_ops", "idempotency_key": ""},
    {"receive_id": "oc_ops", "text": " "},
    {"receive_id": "oc_ops", "text": "a" * (150 * 1024)},
])
def test_invalid_messages_fail_before_any_http_call(monkeypatch, kwargs):
    def handler(request):
        pytest.fail("Invalid messages must not make an HTTP request")

    mock_http(monkeypatch, handler)
    arguments = {"text": "test", "idempotency_key": "key", **kwargs}
    with pytest.raises(ValueError):
        FeishuAppClient("cli_test", "secret").send_text(**arguments)


def test_settings_can_load_private_app_credentials_without_exposing_them(tmp_path):
    path = tmp_path / "private.json"
    path.write_text('{"app_id":"cli_private","app_secret":"test-secret"}', encoding="utf-8")
    settings = SimpleNamespace(
        feishu_app_id="", feishu_app_secret="", feishu_timeout=10,
        feishu_app_credentials_file=str(path),
    )
    client = FeishuAppClient.from_settings(settings)
    assert client._app_id == "cli_private"
    path.write_text('malformed test-secret', encoding="utf-8")
    with pytest.raises(FeishuAppError) as failure:
        FeishuAppClient.from_settings(settings)
    assert "test-secret" not in str(failure.value)


def test_settings_do_not_mix_conflicting_app_credentials(tmp_path):
    path = tmp_path / "private.json"
    path.write_text('{"app_id":"cli_other","app_secret":"test-secret"}', encoding="utf-8")
    settings = SimpleNamespace(
        feishu_app_id="cli_expected", feishu_app_secret="", feishu_timeout=10,
        feishu_app_credentials_file=str(path),
    )
    with pytest.raises(FeishuAppError, match="conflicting app IDs"):
        FeishuAppClient.from_settings(settings)
