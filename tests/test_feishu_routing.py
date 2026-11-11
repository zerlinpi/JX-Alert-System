import json
from jx_alert.notifications.worker import (
    _feishu_group_config, _load_routing, _route_chat_id, _routed_open_ids,
)


def test_store_and_entity_open_id_routing(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps({
        "stores": {"store-1": ["ou_store_owner"]},
        "entities": {"store-1:ASIN1": ["ou_product_owner"], "ASIN2": ["ou_global_owner"]},
    }), encoding="utf-8")
    routing = _load_routing(str(path))
    ids = _routed_open_ids(routing, "store-1", "ASIN1")
    assert ids == ["ou_store_owner", "ou_product_owner"]


def test_invalid_routing_id_is_rejected(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text('{"stores":{"store-1":["not-an-open-id"]}}', encoding="utf-8")
    try:
        _load_routing(str(path))
    except RuntimeError as exc:
        assert "Open ID" in str(exc)
    else:
        raise AssertionError("invalid routing ID should be rejected")



def test_routes_alert_to_entity_group_and_merges_store_and_entity_owners(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps({
        "default_chat_id": "main",
        "groups": {
            "main": {"webhook_env": "FEISHU_WEBHOOK_URL"},
            "store-1-ops": {
                "webhook_env": "FEISHU_WEBHOOK_URL_STORE1",
                "sign_secret_env": "FEISHU_SIGN_SECRET_STORE1",
                "admin_open_ids": ["ou_group_admin"],
            },
        },
        "stores": {
            "store-1": {
                "chat_id": "store-1-ops",
                "open_ids": ["ou_store_owner"],
            },
        },
        "entities": {
            "store-1:SKU-1": {
                "chat_id": "store-1-ops",
                "open_ids": ["ou_product_owner"],
            },
        },
    }), encoding="utf-8")
    routing = _load_routing(str(path))
    assert _route_chat_id(routing, "store-1", "SKU-1") == "store-1-ops"
    assert _routed_open_ids(routing, "store-1", "SKU-1") == [
        "ou_store_owner", "ou_product_owner",
    ]


def test_group_webhook_secrets_are_loaded_from_container_environment(
    tmp_path, monkeypatch
):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps({
        "default_chat_id": "main",
        "groups": {
            "main": {"webhook_env": "FEISHU_WEBHOOK_URL"},
            "eu": {
                "webhook_env": "FEISHU_WEBHOOK_URL_EU",
                "sign_secret_env": "FEISHU_SIGN_SECRET_EU",
                "keyword_env": "FEISHU_KEYWORD_EU",
                "admin_open_ids": ["ou_eu_admin"],
            },
        },
    }), encoding="utf-8")
    routing = _load_routing(str(path))
    monkeypatch.setenv(
        "FEISHU_WEBHOOK_URL_EU",
        "https://open.feishu.cn/open-apis/bot/v2/hook/eu-test",
    )
    monkeypatch.setenv("FEISHU_SIGN_SECRET_EU", "eu-secret")
    monkeypatch.setenv("FEISHU_KEYWORD_EU", "JX Alert")
    group = _feishu_group_config(
        routing,
        "eu",
        type("Settings", (), {
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/main",
            "feishu_sign_secret": "",
            "feishu_keyword": "",
        })(),
    )
    assert group["webhook_url"].endswith("/eu-test")
    assert group["sign_secret"] == "eu-secret"
    assert group["keyword"] == "JX Alert"
    assert group["admin_open_ids"] == ["ou_eu_admin"]
