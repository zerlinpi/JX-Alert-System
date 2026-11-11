from jx_alert.lingxing.token import _json_payload


def test_token_request_supports_local_secret_placeholders():
    result = _json_payload(
        '{"appId":"${LINGXING_APP_ID}","appSecret":"${LINGXING_APP_SECRET}"}',
        {"LINGXING_APP_ID": "id-local", "LINGXING_APP_SECRET": "secret-local"},
    )
    assert result == {"appId": "id-local", "appSecret": "secret-local"}
