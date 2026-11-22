"""DeepSeek owner settings are write-only secrets used at the next analysis."""
import json
import os
import stat

from pydantic import SecretStr
import pytest

from test_admin_console import ORIGIN, console, login  # noqa: F401
from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError
from jx_alert.ai.deepseek_config import cache_identity
from jx_alert.readiness import group_bot_status


ENDPOINT = "/console/api/deepseek"
KEY = "synthetic-only-private-deepseek-key"
NEW_KEY = "synthetic-only-new-deepseek-key"


@pytest.fixture
def deepseek_console(console, tmp_path):  # noqa: F811 - imported fixture reused by pytest
    client, _, _, settings = console
    path = tmp_path / "private" / "deepseek.json"
    settings.deepseek_credentials_file = str(path)
    settings.runtime_settings_file = str(tmp_path / "runtime.json")
    return client, settings, path


def _changes(revision, **overrides):
    return {"revision": revision, "api_key": KEY, "model": "deepseek-v4-pro", "timeout": 31.5,
            **overrides}


def _login(client):
    assert login(client).status_code == 303
    csrf = client.get("/console/api/stores").json()["csrf"]
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def test_owner_csrf_and_same_origin_required_before_receiving_key(deepseek_console):
    client, _, path = deepseek_console
    assert client.get(ENDPOINT).status_code == 401
    assert client.post(ENDPOINT, json=_changes("0" * 64)).status_code == 401
    headers = _login(client)
    assert client.post(ENDPOINT, json=_changes("0" * 64),
                       headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(ENDPOINT, json=_changes("0" * 64),
                       headers={**headers, "Origin": "https://elsewhere.example"}).status_code == 403
    assert not path.exists()


def test_save_redacts_secret_uses_private_file_and_changes_runtime_model(
        deepseek_console, monkeypatch):
    client, settings, path = deepseek_console
    headers = _login(client)
    before = client.get(ENDPOINT)
    assert before.headers["cache-control"] == "no-store"
    assert before.json()["configured"] is False
    revision = before.json()["revision"]
    original_cache_identity = cache_identity(settings)

    def deny_request(*_args, **_kwargs):
        pytest.fail("Reading and saving the admin setup must not call DeepSeek")

    monkeypatch.setattr(DeepSeekClient, "analyze", deny_request)
    response = client.post(ENDPOINT, json=_changes(revision), headers=headers)
    assert response.status_code == 200
    saved = response.json()
    assert saved["status"] == "CONFIGURED"
    assert saved["model"] == "deepseek-v4-pro"
    assert saved["timeout"] == 31.5
    assert saved["updated_at"]
    assert saved["revision"] != revision
    assert "api_key" not in saved and KEY not in response.text and str(path) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert KEY not in client.get("/console/").text
    reread = client.get(ENDPOINT)
    assert reread.json() == saved and KEY not in reread.text
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["api_key"] == KEY
    actual = DeepSeekClient.from_settings(settings)
    assert (actual.model, actual.timeout, actual._api_key) == ("deepseek-v4-pro", 31.5, KEY)
    assert group_bot_status(settings)["deepseek_model"] == actual.model
    assert group_bot_status(settings)["deepseek_credentials_present"] is True
    assert cache_identity(settings) != original_cache_identity
    assert KEY not in cache_identity(settings)
    assert client.post(ENDPOINT, json=_changes(revision, api_key=NEW_KEY),
                       headers=headers).status_code == 409
    assert json.loads(path.read_text())["api_key"] == KEY


def test_legacy_credentials_and_environment_keep_precedence_until_explicit_rotation(deepseek_console):
    client, settings, path = deepseek_console
    settings.deepseek_api_key = SecretStr(KEY)
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({"api_key": "legacy-key-in-file"}))
    assert DeepSeekClient.from_settings(settings)._api_key == KEY
    headers = _login(client)
    legacy = client.get(ENDPOINT).json()
    assert legacy["configured"] and legacy["updated_at"] is None
    saved = client.post(ENDPOINT, json=_changes(legacy["revision"], api_key=""), headers=headers)
    assert saved.status_code == 200
    assert "api_key" not in json.loads(path.read_text())
    assert DeepSeekClient.from_settings(settings)._api_key == KEY
    rotated = client.post(ENDPOINT, json=_changes(saved.json()["revision"], api_key=NEW_KEY), headers=headers)
    assert rotated.status_code == 200
    assert DeepSeekClient.from_settings(settings)._api_key == NEW_KEY
    assert KEY not in rotated.text and NEW_KEY not in rotated.text


def test_blank_key_keeps_existing_legacy_file_key_when_no_environment_key(deepseek_console):
    client, settings, path = deepseek_console
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({"api_key": KEY}))
    headers = _login(client)
    legacy = client.get(ENDPOINT).json()
    result = client.post(ENDPOINT, json=_changes(legacy["revision"], api_key=""), headers=headers)
    assert result.status_code == 200
    assert DeepSeekClient.from_settings(settings)._api_key == KEY
    assert KEY not in result.text


@pytest.mark.parametrize("changes", [
    {"api_key": "newline\r\nheader:" + KEY}, {"model": "https://example.invalid"},
    {"timeout": True}, {"timeout": 121}, {"api_key": ""}, {"path": "/tmp/not-allowed"},
])
def test_invalid_values_do_not_persist_or_echo_submitted_key(deepseek_console, changes):
    client, _, path = deepseek_console
    headers = _login(client)
    result = client.post(ENDPOINT, json=_changes("0" * 64, **changes), headers=headers)
    assert result.status_code == 400
    assert KEY not in result.text and str(path) not in result.text
    assert not path.exists()


def test_untrusted_symlink_prevents_admin_and_runtime_read_write(deepseek_console, tmp_path):
    client, settings, path = deepseek_console
    target = tmp_path / "unrelated"
    target.write_text(json.dumps({"api_key": KEY}))
    path.parent.mkdir(mode=0o700)
    path.symlink_to(target)
    headers = _login(client)
    response = client.get(ENDPOINT)
    assert response.status_code == 503 and KEY not in response.text
    assert client.post(ENDPOINT, json=_changes("0" * 64), headers=headers).status_code == 503
    assert path.is_symlink()
    assert json.loads(target.read_text())["api_key"] == KEY
    with pytest.raises(DeepSeekError) as caught:
        DeepSeekClient.from_settings(settings)
    assert KEY not in str(caught.value)


def test_analysis_settings_page_links_static_script_and_password_field(deepseek_console):
    client, _, _ = deepseek_console
    _login(client)
    page = client.get("/console/")
    assert 'data-view="analysis-settings"' in page.text
    assert 'id="deepseek-key" type="password"' in page.text
    assert 'id="review-skills-panel"' in page.text
    assert page.text.index("/console/assets/review-skills.js") < page.text.index("/console/assets/console.js")
    assert client.get("/console/assets/review-skills.js").status_code == 200
