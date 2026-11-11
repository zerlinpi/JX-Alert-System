import base64
import hashlib
import hmac

from jx_alert.notifications.feishu import FeishuClient, build_feishu_signature


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
