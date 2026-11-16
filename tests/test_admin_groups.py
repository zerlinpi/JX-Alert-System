from copy import deepcopy
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from jx_alert import admin_groups as groups, admin_mapping as mapping


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "routing.json"
    data = {"default_chat_id": "report", "groups": {
        "report": {"chat_id": "oc_old", "display_name": "原群", "admin_open_ids": ["ou_keep", "ou_remove"]},
        "other": {"chat_id": "oc_other", "admin_open_ids": ["ou_other"]}},
        "stores": {"5174": {"open_ids": ["ou_operator"], "manager_open_ids": ["ou_manager"]},
                   "5175": {"chat_id": "other", "open_ids": ["ou_other"]}},
        "entities": {"5174:SKU": ["ou_product"]}, "management": {"open_ids": ["ou_manager"]},
        "unknown": {"preserve": True}}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    settings = SimpleNamespace(feishu_routing_file=str(path), feishu_transport="app",
                               feishu_daily_report_chat_id="report", feishu_group_fallback_enabled=True)
    monkeypatch.setattr(groups, "_joined_chats", lambda *_, **kw: {"oc_new": "新通知群", "oc_old": "原群"})
    monkeypatch.setattr(mapping, "_members", lambda *_: {"ou_keep": "已映射管理员", "ou_joined": "新成员"})
    return path, data, settings


def test_switch_is_explicit_atomic_and_preserves_people_and_unrelated_routes(registry, caplog):
    path, original, settings = registry
    raw = path.read_bytes()
    revision = groups.current_notification_group(settings)["revision"]
    preview = groups.preview_notification_group(settings, "oc_new", revision)
    assert preview["name"] == "新通知群" and preview["removed_administrators"] == 1
    assert preview["retained_administrators"] == 1 and preview["outside_owner_count"] == 2
    assert path.read_bytes() == raw
    with pytest.raises(mapping.MappingError, match="确认"):
        groups.save_notification_group(settings, "oc_new", revision, False)
    with caplog.at_level("INFO", logger=groups.__name__):
        saved = groups.save_notification_group(settings, "oc_new", revision, True)
    assert saved["chat_id"] == "oc_new" and saved["revision"] != revision
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = data["groups"]["report"]
    assert changed["admin_open_ids"] == ["ou_keep"]
    assert changed["verified_member_open_ids"] == ["ou_joined", "ou_keep"]
    assert changed["retired_chat_ids"] == {"oc_old": changed["notifications_changed_at"]}
    for key in ("stores", "entities", "management", "unknown", "default_chat_id"):
        assert data[key] == original[key]
    assert data["groups"]["other"] == original["groups"]["other"]
    [backup] = path.parent.glob("routing.json.before-console-*.json")
    assert backup.read_bytes() == raw
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    assert "ou_" not in caplog.text and "oc_" not in caplog.text


def test_same_group_refresh_does_not_retire_new_messages_and_roundtrip_retains_cutoffs(registry):
    path, _, settings = registry
    saved = groups.save_notification_group(settings, "oc_new", groups.current_notification_group(settings)["revision"], True)
    first = json.loads(path.read_text(encoding="utf-8"))["groups"]["report"]
    saved = groups.save_notification_group(settings, "oc_new", saved["revision"], True)
    assert json.loads(path.read_text(encoding="utf-8"))["groups"]["report"] == first
    groups.save_notification_group(settings, "oc_old", saved["revision"], True)
    latest = json.loads(path.read_text(encoding="utf-8"))["groups"]["report"]
    assert latest["retired_chat_ids"]["oc_old"] == first["retired_chat_ids"]["oc_old"]
    assert "oc_new" in latest["retired_chat_ids"]


def test_stale_or_concurrent_selection_never_overwrites_other_edits(registry, monkeypatch):
    path, original, settings = registry
    revision = groups.current_notification_group(settings)["revision"]
    with pytest.raises(mapping.MappingError) as exc:
        groups.save_notification_group(settings, "oc_new", "0" * 64, True)
    assert exc.value.status_code == 409
    def concurrent(*_):
        altered = deepcopy(original)
        altered["concurrent"] = True
        path.write_text(json.dumps(altered), encoding="utf-8")
        return {"ou_keep": "管理员"}
    monkeypatch.setattr(mapping, "_members", concurrent)
    with pytest.raises(mapping.MappingError) as exc:
        groups.save_notification_group(settings, "oc_new", revision, True)
    assert exc.value.status_code == 409
    assert json.loads(path.read_text(encoding="utf-8"))["concurrent"] is True
    assert groups.current_notification_group(settings)["chat_id"] == "oc_old"


def test_lookup_failure_and_missing_safe_fallback_leave_file_unchanged(registry, monkeypatch):
    path, _, settings = registry
    raw = path.read_bytes()
    revision = groups.current_notification_group(settings)["revision"]
    def fail(*_):
        raise mapping.MappingError(502, "无法读取成员")
    monkeypatch.setattr(mapping, "_members", fail)
    with pytest.raises(mapping.MappingError):
        groups.save_notification_group(settings, "oc_new", revision, True)
    assert path.read_bytes() == raw
    monkeypatch.setattr(mapping, "_members", lambda *_: {})
    settings.feishu_group_fallback_enabled = False
    with pytest.raises(mapping.MappingError) as exc:
        groups.save_notification_group(settings, "oc_new", revision, True)
    assert exc.value.status_code == 400 and path.read_bytes() == raw


def test_owner_save_refreshes_verified_members_without_losing_retirement(registry):
    path, _, settings = registry
    saved = groups.save_notification_group(settings, "oc_new", groups.current_notification_group(settings)["revision"], True)
    mapping.save_store_mapping(settings, "5174", ["ou_joined"], saved["revision"])
    group = json.loads(path.read_text(encoding="utf-8"))["groups"]["report"]
    assert "ou_joined" in group["verified_member_open_ids"] and "oc_old" in group["retired_chat_ids"]


def client_mock(monkeypatch, handler):
    tokens = []
    def token(*, rejected_token=None):
        tokens.append(rejected_token)
        return "replaced-token" if rejected_token else "private-test-token"
    client = SimpleNamespace(base_url="https://open.feishu.cn", timeout=3, _get_token=token,
                             _EXPIRED_TOKEN_CODE=99991663)
    monkeypatch.setattr(groups.FeishuAppClient, "from_settings", lambda _: client)
    real = httpx.Client
    monkeypatch.setattr(groups.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return tokens


def test_joined_groups_paginate_refresh_token_and_validate_selected_chat_mode(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, json={"code": 99991663})
        if request.url.path.endswith("/oc_new"):
            return httpx.Response(200, json={"code": 0, "data": {"chat_mode": "group", "name": "已核实名称"}})
        if request.url.params.get("page_token"):
            return httpx.Response(200, json={"code": 0, "data": {"has_more": False,
                "items": [{"chat_id": "oc_new", "name": "新群", "tenant_key": "hidden"}]}})
        return httpx.Response(200, json={"code": 0, "data": {"has_more": True, "page_token": "next",
            "items": [{"chat_id": "oc_old", "name": "原群"}]}})
    tokens = client_mock(monkeypatch, handler)
    chats = groups._joined_chats(SimpleNamespace(), selected="oc_new")
    assert chats == {"oc_old": "原群", "oc_new": "已核实名称"} and "hidden" not in str(chats)
    assert "private-test-token" in tokens and len(calls) == 4
    assert calls[0].url.params["sort_type"] == "ByCreateTimeAsc"


@pytest.mark.parametrize("kind", ["p2p", "foreign", "loop", "malformed", "rejected"])
def test_incomplete_or_unjoined_or_private_destination_cannot_be_selected(monkeypatch, kind):
    def handler(request):
        if kind == "rejected":
            return httpx.Response(403, json={"code": 999, "msg": "private-test-token"})
        if request.url.path.endswith("/oc_new"):
            return httpx.Response(200, json={"code": 0, "data": {"chat_mode": "p2p"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "has_more": True if kind == "loop" else "invalid" if kind == "malformed" else False,
            "page_token": "repeat", "items": [{"chat_id": "oc_other" if kind == "foreign" else "oc_new", "name": "群"}]}})
    client_mock(monkeypatch, handler)
    with pytest.raises(mapping.MappingError) as exc:
        groups._joined_chats(SimpleNamespace(), selected="oc_new")
    assert exc.value.status_code == (400 if kind in {"p2p", "foreign"} else 502)
    assert "private-test-token" not in str(exc.value)
