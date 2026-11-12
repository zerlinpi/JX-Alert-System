import base64
import hashlib
import hmac
import httpx
import pytest

from jx_alert.notifications.feishu import FeishuClient, FeishuWebhookError, build_feishu_signature


def test_feishu_signature_is_hmac_sha256_base64():
    signature = build_feishu_signature("1700000000", "secret")
    assert isinstance(signature, str)
    assert len(signature) > 20


def test_custom_bot_payload_supports_open_id_mentions_and_keyword():
    client = FeishuClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/not-a-real-secret",
        sign_secret="local-test-secret",
        keyword="告警",
    )
    payload = client._payload("订单异常", ["ou_test_member"])
    text = payload["content"]["text"]
    assert "告警" in text
    assert '<at user_id="ou_test_member">提醒</at>' in text
    assert payload["sign"] == build_feishu_signature(payload["timestamp"], "local-test-secret")


def test_custom_bot_rejects_non_feishu_webhook():
    try:
        FeishuClient("https://example.com/hook/abc")
    except ValueError as exc:
        assert "FEISHU_WEBHOOK_URL" in str(exc)
    else:
        raise AssertionError("invalid webhook URL should be rejected")



def test_feishu_signature_matches_official_newline_formula():
    timestamp = "1700000000"
    secret = "local-test-secret"
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    ).decode("ascii")
    assert build_feishu_signature(timestamp, secret) == expected


def test_webhook_message_data_cannot_inject_mentions():
    client = FeishuClient("https://open.feishu.cn/open-apis/bot/v2/hook/test")
    payload = client._payload('<at user_id="all">unexpected</at>', ["ou_owner"])
    assert '<at user_id="all">' not in payload["content"]["text"]
    assert '<at user_id="ou_owner">' in payload["content"]["text"]
    with pytest.raises(ValueError, match="valid Open IDs"):
        client._payload("test", ['ou_owner\"><at user_id="all'])


@pytest.mark.parametrize("body", [
    {"code": 19021, "msg": "provider echoed a private-webhook-token"},
    {"code": "private-webhook-token"},
    {"code": False},
])
def test_webhook_provider_errors_do_not_reveal_response_contents(monkeypatch, body):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kwargs,
    ))
    with pytest.raises(FeishuWebhookError) as error:
        FeishuClient("https://open.feishu.cn/open-apis/bot/v2/hook/test").send_text("test")
    assert "private-webhook-token" not in str(error.value)
