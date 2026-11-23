"""Owner-confirmed replacement of the existing notification group alias.

Official contracts:
https://open.feishu.cn/document/server-docs/group/chat/list
https://open.feishu.cn/document/server-docs/group/chat/get-2
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path

import httpx

from jx_alert import admin_mapping as mapping
from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError


_LOG = logging.getLogger(__name__)
_GROUP_ERROR = "无法读取机器人所在群。请确认已开启查看群信息权限，并将机器人加入目标群后刷新。"


def _request(http, client, path, params=None):
    token = client._get_token()
    for attempt in range(2):
        response = http.get(client.base_url + path, params=params,
                            headers={"Authorization": "Bearer " + token})
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("code") == client._EXPIRED_TOKEN_CODE and attempt == 0:
            token = client._get_token(rejected_token=token)
            continue
        if response.status_code != 200 or type(payload.get("code")) is not int or payload["code"] != 0:
            raise ValueError
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError
        return data
    raise ValueError


def _joined_chats(settings, *, selected=None):
    """List joined groups, never the wider search-visible directory or p2p chats."""
    try:
        client = FeishuAppClient.from_settings(settings)
        with httpx.Client(timeout=client.timeout, follow_redirects=False) as http:
            params = {"page_size": 100, "sort_type": "ByCreateTimeAsc"}
            chats, seen = {}, set()
            for _ in range(100):
                data = _request(http, client, "/open-apis/im/v1/chats", params)
                if not isinstance(data.get("items"), list) or type(data.get("has_more")) is not bool:
                    raise ValueError
                for item in data["items"]:
                    chat_id = item.get("chat_id") if isinstance(item, dict) else None
                    if not mapping._valid_chat(chat_id):
                        raise ValueError
                    chats[chat_id] = mapping._label(item.get("name"), "未命名群")
                if not data["has_more"]:
                    break
                cursor = data.get("page_token")
                if not isinstance(cursor, str) or not cursor.strip() or cursor in seen:
                    raise ValueError
                seen.add(cursor)
                params["page_token"] = cursor
            else:
                raise ValueError
            if selected is not None:
                if selected not in chats:
                    raise mapping.MappingError(400, "目标群不在机器人已加入的群列表中，请先添加机器人并刷新。")
                detail = _request(http, client, "/open-apis/im/v1/chats/" + selected)
                if detail.get("chat_mode") not in {"group", "topic"}:
                    raise mapping.MappingError(400, "通知目标必须是群聊，不能使用单聊。")
                chats[selected] = mapping._label(detail.get("name"), chats[selected])
            return chats
    except mapping.MappingError:
        raise
    except (FeishuAppError, httpx.HTTPError, ValueError, OSError):
        raise mapping.MappingError(502, _GROUP_ERROR) from None


def _snapshot(settings):
    if getattr(settings, "feishu_transport", "webhook") != "app":
        raise mapping.MappingError(503, "替换通知群需要使用飞书自建应用。")
    raw, routing, revision = mapping._routing_snapshot(Path(settings.feishu_routing_file))
    alias = getattr(settings, "feishu_daily_report_chat_id", "")
    group = routing.get("groups", {}).get(alias)
    if not alias or not isinstance(group, dict) or not mapping._valid_chat(group.get("chat_id")):
        raise mapping.MappingError(503, "当前日报群配置无效，请先核对运行配置。")
    return raw, routing, revision, alias, group


def _current(routing, revision, alias, group):
    explicit = sum(1 for entry in routing.get("stores", {}).values()
                   if isinstance(entry, dict) and entry.get("chat_id") not in (None, alias))
    return {"chat_id": group["chat_id"], "name": mapping._group_label(group, alias),
            "revision": revision, "other_store_routes": explicit}


def current_notification_group(settings):
    _, routing, revision, alias, group = _snapshot(settings)
    return _current(routing, revision, alias, group)


def list_notification_groups(settings):
    current = current_notification_group(settings)
    chats = _joined_chats(settings)
    return {**current, "chats": [{"id": uid, "name": name} for uid, name in
                                  sorted(chats.items(), key=lambda item: (item[1].casefold(), item[0]))]}


def _replacement(settings, chat_id, revision):
    if not mapping._valid_chat(chat_id):
        raise mapping.MappingError(400, "请选择有效的通知群。")
    if not isinstance(revision, str) or not mapping._REVISION.fullmatch(revision):
        raise mapping.MappingError(400, "群配置版本无效，请刷新后重试。")
    raw, routing, current, alias, group = _snapshot(settings)
    if current != revision:
        raise mapping.MappingError(409, "群或人员映射已更新，请刷新后重新选择。")
    chats = _joined_chats(settings, selected=chat_id)
    verified_app = mapping.scoped_app(settings)
    roster = (mapping._members(settings, chat_id, app_id=verified_app) if verified_app
              else mapping._members(settings, chat_id))
    admins = list(dict.fromkeys(group.get("admin_open_ids", [])))
    retained = [uid for uid in admins if uid in roster]
    outside = set()
    for section in ("stores", "entities"):
        for entry in routing.get(section, {}).values():
            owners = entry if isinstance(entry, list) else entry.get("open_ids", [])
            target_alias = entry.get("chat_id") if isinstance(entry, dict) else None
            if target_alias is None:
                target_alias = routing["default_chat_id"]
            if target_alias == alias:
                outside.update(uid for uid in owners if uid not in roster)
    if not retained and not getattr(settings, "feishu_group_fallback_enabled", False):
        raise mapping.MappingError(400, "新群没有已映射管理员，且群兜底未启用；请先核实通知路由。")
    preview = {"chat_id": chat_id, "name": chats[chat_id], "revision": revision,
               "previous_name": mapping._group_label(group, alias),
               "retained_administrators": len(retained), "removed_administrators": len(admins) - len(retained),
               "outside_owner_count": len(outside), "changed": chat_id != group["chat_id"]}
    return raw, routing, alias, group, retained, roster, verified_app, preview


def preview_notification_group(settings, chat_id, revision):
    return _replacement(settings, chat_id, revision)[-1]


def save_notification_group(settings, chat_id, revision, confirmed):
    if confirmed is not True:
        raise mapping.MappingError(400, "请先确认通知群替换范围。")
    raw, routing, alias, group, admins, roster, verified_app, preview = _replacement(settings, chat_id, revision)
    updated = dict(group)
    updated.update(chat_id=chat_id, display_name=preview["name"], admin_open_ids=admins,
                   verified_member_open_ids=sorted(roster))
    if verified_app:
        updated["hourly_verified_roster_app_id"] = verified_app
    else:
        updated.pop("hourly_verified_roster_app_id", None)
    if preview["changed"]:
        now = datetime.now(timezone.utc).isoformat()
        retired = dict(updated.get("retired_chat_ids", {}))
        retired[group["chat_id"]] = now
        updated.update(notifications_changed_at=now, retired_chat_ids=retired)
    routing["groups"][alias] = updated
    payload = (json.dumps(routing, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = Path(settings.feishu_routing_file)
    try:
        with mapping._mapping_lock(path):
            if path.is_symlink() or path.read_bytes() != raw:
                raise mapping.MappingError(409, "群或人员映射已更新，请刷新后重新选择。")
            if updated != group:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                mapping._replace_mapping(path, raw, payload, "notification-group", stamp)
    except OSError:
        raise mapping.MappingError(503, "通知群保存失败，请核对运行目录权限。") from None
    _LOG.info("Notification group selection saved")
    result = _current(routing, hashlib.sha256(payload if updated != group else raw).hexdigest(), alias, updated)
    return {**result, "impact": preview}
