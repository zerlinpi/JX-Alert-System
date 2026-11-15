from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jx_alert import admin_mapping as mapping


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "feishu-routing.json"
    data = {
        "groups": {
            "report": {"chat_id": "oc_report", "display_name": "日报群", "admin_open_ids": ["ou_admin"]},
            "operations": {"chat_id": "oc_operations", "display_name": "运营群", "admin_open_ids": ["ou_admin2"]},
        },
        "default_chat_id": "report",
        "stores": {"5174": {"chat_id": "operations", "open_ids": ["ou_old"], "manager_open_ids": ["ou_mgmt"]},
                   "5175": ["ou_other"]},
        "entities": {"5174:SKU": {"open_ids": ["ou_product"]}},
        "management": {"open_ids": ["ou_management"]},
        "other_config": {"keep": [1, "original"]},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    settings = SimpleNamespace(feishu_routing_file=str(path), feishu_transport="app",
                               feishu_daily_report_chat_id="report", feishu_group_fallback_enabled=True)
    return path, data, settings


def test_verified_store_save_retains_unrelated_routes_and_creates_private_backup(registry, monkeypatch, caplog):
    path, data, settings = registry
    original = path.read_bytes()
    if os.name != "nt":
        path.chmod(0o640)
    def members(received_settings, chat):
        assert received_settings is settings
        assert chat == "oc_operations"
        return {"ou_new": "运营甲", "ou_second": "运营乙"}
    monkeypatch.setattr(mapping, "_members", members)
    current = mapping.get_store_mapping(settings, "5174")
    assert current["group"] == "运营群" and current["fallback_group"] == "日报群"
    assert current["owners"] == [{"id": "ou_old", "name": "ou_old"}]
    assert mapping.get_store_members(settings, 5174)["revision"] == current["revision"]
    with caplog.at_level("INFO", logger=mapping.__name__):
        saved = mapping.save_store_mapping(settings, "5174", ["ou_new", "ou_new", "ou_second"], current["revision"])
    assert saved["revision"] != current["revision"]
    assert saved == mapping.get_store_mapping(settings, 5174)
    assert saved["owners"] == [{"id": "ou_new", "name": "运营甲"}, {"id": "ou_second", "name": "运营乙"}]
    assert mapping.store_mapping_summaries(settings, ["5174", "5175"]) == {
        "5174": {"owner_count": 2, "group": "运营群"}, "5175": {"owner_count": 1, "group": "日报群"}}
    expected = deepcopy(data)
    expected["stores"]["5174"].update(open_ids=["ou_new", "ou_second"],
                                        owner_display_names={"ou_new": "运营甲", "ou_second": "运营乙"})
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    [backup] = path.parent.glob(path.name + ".before-console-*.json")
    assert "-5174-" in backup.name
    assert backup.read_bytes() == original
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o640
    assert "store=5174" in caplog.text and "ou_" not in caplog.text
    assert "运营甲" not in caplog.text


def test_stale_and_unverified_members_cannot_change_routing(registry, monkeypatch):
    path, data, settings = registry
    revision = mapping.get_store_mapping(settings, "5174")["revision"]
    raw = path.read_bytes()
    monkeypatch.setattr(mapping, "_members", lambda *_: {"ou_old": "原负责人"})
    with pytest.raises(mapping.MappingError) as error:
        mapping.save_store_mapping(settings, "5174", ["ou_unknown"], revision)
    assert error.value.status_code == 400
    assert path.read_bytes() == raw

    def concurrent_edit(*_):
        altered = {**data, "unrelated": True}
        path.write_text(json.dumps(altered), encoding="utf-8")
        return {"ou_new": "群内负责人"}
    monkeypatch.setattr(mapping, "_members", concurrent_edit)
    with pytest.raises(mapping.MappingError) as error:
        mapping.save_store_mapping(settings, "5174", ["ou_new"], revision)
    assert error.value.status_code == 409
    assert json.loads(path.read_text(encoding="utf-8"))["unrelated"] is True
    assert mapping.get_store_mapping(settings, "5174")["owners"][0]["id"] == "ou_old"


def test_clearing_requires_explicit_report_group_fallback(registry, monkeypatch):
    _, _, settings = registry
    def fail_if_called(*_):
        pytest.fail("Clearing local mapping must not read a directory")
    monkeypatch.setattr(mapping, "_members", fail_if_called)
    revision = mapping.get_store_mapping(settings, "5174")["revision"]
    settings.feishu_group_fallback_enabled = False
    with pytest.raises(mapping.MappingError) as error:
        mapping.save_store_mapping(settings, "5174", [], revision)
    assert error.value.status_code == 400
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "absent"
    with pytest.raises(mapping.MappingError) as error:
        mapping.save_store_mapping(settings, "5174", [], revision)
    assert error.value.status_code == 400
    settings.feishu_daily_report_chat_id = "report"
    saved = mapping.save_store_mapping(settings, "5174", [], revision)
    assert saved["owners"] == [] and saved["fallback_group"] == "日报群"
    assert saved["group"] == "运营群"


def test_missing_default_group_target_uses_configured_report_group(registry, monkeypatch):
    path, data, settings = registry
    data["groups"]["webhook_only"] = {"webhook_env": "FEISHU_WEBHOOK_URL"}
    data["default_chat_id"] = "webhook_only"
    path.write_text(json.dumps(data), encoding="utf-8")
    requests = []
    def members(_, group):
        requests.append(group)
        return {"ou_new": "新负责人"}
    monkeypatch.setattr(mapping, "_members", members)
    current = mapping.get_store_mapping(settings, "5175")
    assert current["group"] == "日报群"
    saved = mapping.save_store_mapping(settings, "5175", ["ou_new"], current["revision"])
    assert saved["group"] == "日报群" and requests == ["oc_report"]
    assert json.loads(path.read_text(encoding="utf-8"))["stores"]["5175"]["chat_id"] == "report"


def test_member_lookup_paginates_visible_chat_and_returns_only_allowlisted_fields(registry, monkeypatch):
    _, _, settings = registry
    tokens = []
    def token(*, rejected_token=None):
        tokens.append(rejected_token)
        return "test-token" if not rejected_token else "new-token"
    client = SimpleNamespace(base_url="https://open.feishu.cn", timeout=3, _get_token=token,
                             _EXPIRED_TOKEN_CODE=99991663)
    monkeypatch.setattr(mapping.FeishuAppClient, "from_settings", lambda _: client)
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.path == "/open-apis/im/v1/chats/oc_operations/members"
        assert request.url.params["member_id_type"] == "open_id"
        if len(requests) == 1:
            return httpx.Response(401, json={"code": 99991663})
        assert request.headers["authorization"] == "Bearer new-token"
        if len(requests) == 2:
            return httpx.Response(200, json={"code": 0, "data": {"has_more": True, "page_token": "second",
                "items": [{"member_id": "ou_first", "name": "  Alice\u202e ", "tenant_key": "private"}]}})
        assert request.url.params["page_token"] == "second"
        return httpx.Response(200, json={"code": 0, "data": {"has_more": False,
            "items": [{"member_id": "ou_second", "name": "Ben"}, {"member_id": "unverified", "name": "Hidden"}]}})
    http_client = httpx.Client
    monkeypatch.setattr(mapping.httpx, "Client", lambda **kw: http_client(transport=httpx.MockTransport(handle), **kw))
    members = mapping.get_store_members(settings, "5174")
    assert members["members"] == [{"id": "ou_first", "name": "Alice"}, {"id": "ou_second", "name": "Ben"}]
    assert tokens == [None, "test-token"] and len(requests) == 3
    assert "private" not in str(members)


def test_pagination_failure_keeps_owner_file_and_returns_safe_error(registry, monkeypatch):
    path, _, settings = registry
    client = SimpleNamespace(base_url="https://open.feishu.cn", timeout=3, _get_token=lambda: "test-token",
                             _EXPIRED_TOKEN_CODE=99991663)
    monkeypatch.setattr(mapping.FeishuAppClient, "from_settings", lambda _: client)
    def handle(_):
        return httpx.Response(200, json={"code": 0, "data": {"has_more": True, "page_token": "repeated",
            "items": [{"member_id": "ou_new", "name": "private-person"}]}})
    http_client = httpx.Client
    monkeypatch.setattr(mapping.httpx, "Client", lambda **kw: http_client(transport=httpx.MockTransport(handle), **kw))
    raw = path.read_bytes()
    with pytest.raises(mapping.MappingError) as error:
        mapping.save_store_mapping(settings, "5174", ["ou_new"], mapping.get_store_mapping(settings, "5174")["revision"])
    assert error.value.status_code == 502
    assert "private-person" not in str(error.value) and "test-token" not in str(error.value)
    assert path.read_bytes() == raw
    assert not list(Path(path.parent).glob("*.before-console-*.json"))
