import json
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.readiness import feishu_channel_status


def settings(tmp_path, **overrides):
    values = dict(feishu_transport="app", feishu_app_id="", feishu_app_secret=SecretStr(""),
                  feishu_app_credentials_file=str(tmp_path / "private-app.json"), feishu_timeout=2,
                  feishu_webhook_url="", feishu_enabled=True, app_dry_run=False)
    return SimpleNamespace(**(values | overrides))


def test_app_file_credentials_are_ready_without_webhook_or_network(tmp_path, monkeypatch):
    config = settings(tmp_path)
    with open(config.feishu_app_credentials_file, "w") as stream:
        json.dump({"app_id": "cli_synthetic", "app_secret": "synthetic-private-value"}, stream)
    def forbidden(*args, **kwargs):
        pytest.fail("Health must not call Feishu or send messages")
    monkeypatch.setattr(FeishuAppClient, "_get_token", forbidden)
    monkeypatch.setattr(FeishuAppClient, "_post_json", forbidden)
    result = feishu_channel_status(config)
    assert result["feishu_transport"] == "app"
    assert result["feishu_transport_configured"] and result["feishu_delivery_enabled"]
    assert result["feishu_app_credentials_configured"] and not result["feishu_webhook_required"]
    assert result["feishu_check_scope"] == "configuration_only"
    assert "不需要Webhook" in result["feishu_status_note"]
    serialized = json.dumps(result)
    assert "synthetic-private-value" not in serialized and config.feishu_app_credentials_file not in serialized


@pytest.mark.parametrize("enabled,dry,expected", [(True, False, True), (False, False, False), (True, True, False)])
def test_app_direct_credentials_configuration_is_separate_from_execution(tmp_path, enabled, dry, expected):
    result = feishu_channel_status(settings(tmp_path, feishu_app_id="cli_synthetic",
        feishu_app_secret=SecretStr("synthetic-secret"), feishu_enabled=enabled, app_dry_run=dry))
    assert result["feishu_transport_configured"]
    assert result["feishu_delivery_enabled"] is expected


@pytest.mark.parametrize("content", [None, "{bad json", "[]", "{}",
    '{"app_id":"cli_test"}', '{"app_id":" ","app_secret":"synthetic-secret"}'])
def test_invalid_app_never_falls_back_to_configured_webhook(tmp_path, content):
    config = settings(tmp_path, feishu_webhook_url="https://example.test/synthetic-secret")
    if content is not None:
        with open(config.feishu_app_credentials_file, "w") as stream:
            stream.write(content)
    result = feishu_channel_status(config)
    assert not result["feishu_transport_configured"] and not result["feishu_delivery_enabled"]
    assert result["feishu_configuration_status"] == "INVALID_CONFIGURATION"
    assert "synthetic-secret" not in json.dumps(result)


def test_conflicting_app_credentials_do_not_expose_values_or_paths(tmp_path):
    config = settings(tmp_path, feishu_app_id="cli_existing")
    with open(config.feishu_app_credentials_file, "w") as stream:
        json.dump({"app_id": "cli_conflicting", "app_secret": "synthetic-secret"}, stream)
    result = feishu_channel_status(config)
    assert not result["feishu_transport_configured"]
    for value in ["cli_existing", "cli_conflicting", "synthetic-secret", config.feishu_app_credentials_file]:
        assert value not in json.dumps(result)


@pytest.mark.parametrize("mode,url,configured", [("webhook", "", False),
    ("webhook", "https://example.test/synthetic-secret", True),
    ("invalid", "https://example.test/synthetic-secret", False)])
def test_only_selected_supported_transport_is_checked(tmp_path, monkeypatch, mode, url, configured):
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda *_: pytest.fail("Inactive app credentials read"))
    result = feishu_channel_status(settings(tmp_path, feishu_transport=mode, feishu_webhook_url=url))
    assert result["feishu_transport_configured"] is configured
    assert result["feishu_app_credentials_configured"] is None
    assert result["feishu_webhook_required"] is (mode == "webhook")
    assert "synthetic-secret" not in json.dumps(result)


def test_system_health_survives_database_failure_and_reports_selected_channel(tmp_path, monkeypatch):
    from jx_alert import mcp_server
    config = settings(tmp_path, feishu_app_id="cli_synthetic", feishu_app_secret=SecretStr("synthetic-secret"))
    config.__dict__.update(lingxing_access_token="", lingxing_token_path="", lingxing_app_id="",
        lingxing_app_secret="", lingxing_mcp_url="", lingxing_mcp_key=SecretStr(""), scheduler_enabled=True,
        mcp_bearer_token="synthetic-bearer", mcp_path="/mcp", mcp_port=8001)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: config)
    monkeypatch.setattr(mcp_server, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("private-db-error")))
    result = mcp_server.system_health()
    assert result["database"] == "unavailable"
    assert not result["feishu_webhook_configured"] and result["feishu_transport_configured"]
    assert result["feishu_transport"] == "app"
    assert all(value not in json.dumps(result) for value in ["private-db-error", "synthetic-secret", "synthetic-bearer"])
