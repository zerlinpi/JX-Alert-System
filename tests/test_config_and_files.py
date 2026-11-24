"""Runtime settings compatibility and the owner's private files."""
import json
import logging
import os

import pytest

from conftest import write_private
from jx_alert import config
from jx_alert.sparkx import credentials, stores


def test_unused_runtime_fields_are_ignored_by_name_and_renamed_ones_still_apply(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "_REPORTED_IGNORED", set())
    monkeypatch.chdir(tmp_path)  # no .env from the working directory
    monkeypatch.delenv("APP_DRY_RUN", raising=False)
    path = tmp_path / "runtime-settings.json"
    path.write_text(json.dumps({"feishu_enabled": True, "feishu_transport": "app", "hourly_ad_monitor_enabled": True,
                                "mcp_auth_mode": "oauth", "mcp_public_url": "https://jx.example",
                                "excel_archive_enabled": True, "app_dryrun": False}))
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(path))
    with caplog.at_level(logging.WARNING, logger="jx_alert.config"):
        settings = config.load_runtime_settings()
        assert settings.feishu_enabled and settings.console_public_url == "https://jx.example"
        assert settings.app_dry_run and not hasattr(settings, "hourly_ad_monitor_enabled")  # a typo changes nothing
        assert caplog.text.count("ignoring 4 field(s)") == 1
        assert all(name in caplog.text for name in ("app_dryrun", "excel_archive_enabled", "mcp_auth_mode"))
        config.load_runtime_settings()  # the scheduler reloads often: each name is logged once per process
        assert caplog.text.count("ignoring") == 1
    for broken in ("[]", '{"feishu_enabled": true, "feishu_enabled": false}', '{"sparkx_timeout": "slow"}'):
        path.write_text(broken)
        with pytest.raises(ValueError):
            config.load_runtime_settings()


def test_environment_aliases_from_the_previous_release(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://jxmcp.example")
    monkeypatch.setenv("MCP_OWNER_CREDENTIALS_FILE", "/app/data/secrets/mcp-owner.json")
    settings = config.Settings(_env_file=None)
    assert settings.console_public_url == "https://jxmcp.example"
    assert settings.console_owner_credentials_file == "/app/data/secrets/mcp-owner.json"
    assert not settings.delivery_enabled()


def test_console_token_overrides_environment_and_an_unsafe_file_disables(settings):
    assert credentials.effective_token(settings) == "synthetic-sparkx-token"
    view = credentials.configuration(settings)
    saved = credentials.save_configuration(settings, {"revision": view["revision"], "token": "console-token"})
    assert saved["token_source"] == "console" and credentials.effective_token(settings) == "console-token"
    assert "console-token" not in json.dumps(saved)
    with pytest.raises(credentials.SparkXConfigError):
        credentials.save_configuration(settings, {"revision": view["revision"], "token": "other"})  # stale revision
    cleared = credentials.save_configuration(settings, {"revision": saved["revision"], "clear": True})
    assert cleared["token_source"] == "environment"
    with open(settings.sparkx_credentials_file, "w", encoding="utf-8") as stream:
        stream.write("{broken")
    assert credentials.effective_token(settings) == ""


def test_store_directory_validation_and_compare_and_swap(settings):
    directory = stores.load(settings)
    assert stores.monitored(directory, "101") and stores.operator_label(directory, "101") == "未映射运营"
    saved = stores.save(settings, {"101": {"monitored": True, "operators": [{"open_id": "ou_op1", "name": "皮泽霖"}]},
                                   "105": {"monitored": False, "operators": []}}, directory["revision"])
    again = stores.load(settings)
    assert again["revision"] == saved["revision"] and stores.operator_label(again, "101") == "皮泽霖"
    assert not stores.monitored(again, "105")
    if os.name != "nt":
        assert oct(os.stat(settings.sparkx_stores_file).st_mode & 0o777) == "0o600"
    with pytest.raises(stores.StoreDirectoryError) as exc:
        stores.save(settings, {"101": {"monitored": True, "operators": []}}, directory["revision"])
    assert exc.value.status_code == 409
    with pytest.raises(stores.StoreDirectoryError):
        stores.save(settings, {"101": {"monitored": True, "operators": [{"open_id": "bad", "name": "x"}]}},
                    again["revision"])


def test_display_names_follow_the_agent_style():
    names = stores.display_names({"1": {"name": "XPE-主账号", "country": "US"}, "2": {"name": "川页-CA", "country": "CA"},
                                  "3": {"name": "川页-US", "country": "US"}})
    assert names == {"1": "XPE", "2": "川页（加拿大）", "3": "川页"}
    # Live case: a seller and a vendor account whose names differ only in case and punctuation.
    twins = stores.display_names({
        "4": {"name": "聚星同创_Hong Kong Juxing Tongchuang Technology CO., Limited", "country": "US",
              "store_type": "seller"},
        "5": {"name": "聚星同创_HONG KONG JUXING TONGCHUANG TECHNOLOGY CO.,LIMITED", "country": "US",
              "store_type": "vendor"},
        "6": {"name": "XPE", "country": "US", "store_type": "vendor"}})
    assert twins == {"4": "聚星同创_Hong Kong Juxing Tongchuang Technology CO., Limited",
                     "5": "聚星同创_HONG KONG JUXING TONGCHUANG TECHNOLOGY CO.,LIMITED（Vendor）", "6": "XPE"}


def test_private_helper_refuses_group_readable_files(settings, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permissions")
    path = tmp_path / "jx-notifications.json"
    write_private(path, {"version": 1})
    os.chmod(path, 0o644)
    from jx_alert.reports import settings as lane_settings
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.load(settings)
