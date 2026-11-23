"""Runtime settings compatibility and the owner's private files."""
import json
import os

import pytest
from sqlalchemy import text

from conftest import write_private
from jx_alert import config
from jx_alert.sparkx import credentials, stores


def test_retired_runtime_keys_are_ignored_and_renamed_ones_still_apply(tmp_path, monkeypatch):
    path = tmp_path / "runtime-settings.json"
    path.write_text(json.dumps({"feishu_enabled": True, "feishu_transport": "app", "lingxing_sources_file": "x",
                                "hourly_ad_monitor_enabled": True, "mcp_auth_mode": "oauth",
                                "mcp_public_url": "https://jx.example", "excel_archive_enabled": True}))
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(path))
    settings = config.load_runtime_settings()
    assert settings.feishu_enabled and settings.console_public_url == "https://jx.example"
    assert not hasattr(settings, "lingxing_sources_file")
    path.write_text(json.dumps({"unknown_switch": True}))
    with pytest.raises(ValueError):
        config.load_runtime_settings()
    path.write_text('{"feishu_enabled": true, "feishu_enabled": false}')
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


def test_legacy_operator_suggestions_read_the_old_tables_without_writing(session_factory, routing):
    with session_factory() as db:
        db.execute(text("CREATE TABLE stores (sid VARCHAR(20), name VARCHAR(200), country VARCHAR(20), "
                        "marketplace_id VARCHAR(40))"))
        db.execute(text("INSERT INTO stores VALUES ('9060', '川页-US', '美国', 'ATVPDKIKX0DER'), "
                        "('5171', '耀强-Campx-主账号-US', '美国', 'ATVPDKIKX0DER'), ('9061', '川页-CA', '加拿大', NULL)"))
        db.commit()
        old = {**routing, "stores": {"9060": {"open_ids": ["ou_op1"], "owner_display_names": {"ou_op1": "皮泽霖"}},
                                     "5171": ["ou_op2"]}}
        roster = {"101": {"name": "香港川页", "country": "US"}, "102": {"name": "Campx-主账号", "country": "US"},
                  "103": {"name": "OCTORA", "country": "US"}}
        found = stores.legacy_suggestions(db, old, roster)
    assert found == {"101": {"store": "川页-US", "operators": [{"open_id": "ou_op1", "name": "皮泽霖"}]},
                     "102": {"store": "耀强-Campx-主账号-US", "operators": [{"open_id": "ou_op2", "name": "ou_op2"}]}}


def test_legacy_suggestions_without_the_old_table_are_empty(session_factory, routing):
    with session_factory() as db:
        assert stores.legacy_suggestions(db, routing, {"101": {"name": "x", "country": "US"}}) == {}


def test_display_names_follow_the_agent_style():
    names = stores.display_names({"1": {"name": "XPE-主账号", "country": "US"}, "2": {"name": "川页-CA", "country": "CA"},
                                  "3": {"name": "川页-US", "country": "US"}})
    assert names == {"1": "XPE", "2": "川页（加拿大）", "3": "川页"}


def test_private_helper_refuses_group_readable_files(settings, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permissions")
    path = tmp_path / "jx-notifications.json"
    write_private(path, {"version": 1})
    os.chmod(path, 0o644)
    from jx_alert.reports import settings as lane_settings
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.load(settings)
