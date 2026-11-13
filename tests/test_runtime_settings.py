import json

import pytest

from jx_alert.config import get_settings


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "runtime.json"
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(path))
    monkeypatch.setenv("MCP_AUTH_FILE", str(tmp_path / "absent.json"))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_runtime_overrides_flags_and_paths_without_env_changes(runtime):
    runtime.write_text(json.dumps({"scheduler_enabled": True, "app_dry_run": False,
                                  "store_timezones_file": "/data/verified-timezones.json"}))
    settings = get_settings()
    assert settings.scheduler_enabled and not settings.app_dry_run
    assert settings.store_timezones_file == "/data/verified-timezones.json"


def test_runtime_rejects_credentials_and_database_overrides(runtime):
    runtime.write_text(json.dumps({"database_url": "sqlite:///unexpected.db"}))
    with pytest.raises(ValueError, match="unsupported"):
        get_settings()


def test_health_observation_only_is_default_and_explicit_runtime_mode(runtime):
    assert get_settings().lingxing_performance_observation_only is True
    get_settings.cache_clear()
    runtime.write_text(json.dumps({"lingxing_performance_enabled": True,
                                  "lingxing_performance_observation_only": True}))
    settings = get_settings()
    assert settings.lingxing_performance_enabled and settings.lingxing_performance_observation_only
