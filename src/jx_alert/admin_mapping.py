"""Explicit owner edits to the existing Feishu store routing file."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata
from uuid import uuid4

import httpx

from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError
from jx_alert.notifications.hourly_ad_routing import app_fingerprint, scoped_app, scoped_routing_enabled
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.notifications.routing import (
    ApplicationRoutingError, _application_route_entry, _validate_routing_data, report_group_delivery,
)
from jx_alert.storage.job_lock import _try_file_lock, _unlock_file


_LOG = logging.getLogger(__name__)
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,197}")
_CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]{1,197}")
_REVISION = re.compile(r"[a-f0-9]{64}")
_ROUTING_ERROR = "飞书路由配置不可用，请核对群和店铺映射。"
_CONFLICT = "映射已更新，请刷新后再保存。"
_MEMBERS_ERROR = "无法读取飞书群成员。请确认自建应用已加入该群并获准读取群成员。"


class MappingError(RuntimeError):
    """Safe outcome for an authenticated management route."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _store_key(sid) -> str:
    if type(sid) not in (int, str) or not re.fullmatch(r"[1-9][0-9]{0,99}", str(sid)):
        raise MappingError(400, "店铺编号无效。")
    return str(sid)


def _label(value, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    clean = "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf", "Cs"})
    return clean.strip()[:100] or fallback


def _routing_snapshot(path: Path):
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        raw = path.read_bytes()
        routing = _validate_routing_data(json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_fields))
    except (OSError, ValueError, RuntimeError):
        raise MappingError(503, _ROUTING_ERROR) from None
    return raw, routing, hashlib.sha256(raw).hexdigest()


def _report_fallback(settings, routing):
    try:
        report_group_delivery(settings, routing)
    except ApplicationRoutingError:
        return None
    return settings.feishu_daily_report_chat_id


def _group_label(group, alias):
    return _label(group.get("display_name") or group.get("name"), alias)


def _store_route(settings, routing, sid):
    if getattr(settings, "feishu_transport", "webhook") != "app":
        raise MappingError(503, "读取人员映射需要配置飞书自建应用。")
    groups = routing.get("groups", {})
    try:
        route = _application_route_entry(routing.get("stores", {}), sid, groups)
    except ApplicationRoutingError:
        raise MappingError(503, _ROUTING_ERROR) from None
    explicit = route.get("chat_id")
    default = routing["default_chat_id"]
    alias = explicit or default
    fallback = _report_fallback(settings, routing)
    group = groups.get(alias, {})
    chat = group.get("chat_id")
    if not _valid_chat(chat) and not explicit and fallback:
        alias = fallback
        group = groups[alias]
        chat = group["chat_id"]
    if not _valid_chat(chat):
        raise MappingError(503, "这家店铺没有可用的飞书群。请先核对群配置。")
    fallback_group = None
    if fallback and getattr(settings, "feishu_group_fallback_enabled", False):
        fallback_group = _group_label(groups[fallback], fallback)
    return route, alias, chat, _group_label(group, alias), fallback_group


def _valid_chat(chat) -> bool:
    return isinstance(chat, str) and _CHAT_ID.fullmatch(chat) is not None


def _mapping(settings, routing, sid, revision):
    route, _, _, display, fallback_group = _store_route(settings, routing, sid)
    names = route.get("owner_display_names")
    names = names if isinstance(names, dict) else {}
    return {"owners": [{"id": uid, "name": _label(names.get(uid), uid)}
                       for uid in dict.fromkeys(route.get("open_ids", []))],
            "revision": revision, "group": display, "fallback_enabled": fallback_group is not None,
            "fallback_group": fallback_group}


def get_store_mapping(settings, sid):
    sid = _store_key(sid)
    _, routing, revision = _routing_snapshot(Path(settings.feishu_routing_file))
    return _mapping(settings, routing, sid, revision)


def store_mapping_summaries(settings, sids):
    """Parse once for the overview. Detailed requests still validate their route."""
    _, routing, revision = _routing_snapshot(Path(settings.feishu_routing_file))
    result = {}
    for value in sids:
        sid = _store_key(value)
        try:
            summary = _mapping(settings, routing, sid, revision)
        except MappingError:
            result[sid] = {"owner_count": None, "group": None}
        else:
            result[sid] = {"owner_count": len(summary["owners"]), "group": summary["group"]}
    return result


def _members(settings, chat, *, app_id=None):
    """Feishu group member Open IDs, scoped to the currently routed chat."""
    # https://open.feishu.cn/document/server-docs/group/chat-member/get
    try:
        client = FeishuAppClient.from_settings(settings)
        if app_id is not None and client.application_identity != app_fingerprint(app_id):
            raise ValueError("Feishu roster scope changed")
        token = client._get_token()
        params = {"member_id_type": "open_id", "page_size": 100}
        members, seen_tokens = {}, set()
        with httpx.Client(timeout=client.timeout, follow_redirects=False) as http:
            for _ in range(100):
                for attempt in range(2):
                    response = http.get(f"{client.base_url}/open-apis/im/v1/chats/{chat}/members",
                                        params=params, headers={"Authorization": "Bearer " + token})
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError
                    if payload.get("code") == client._EXPIRED_TOKEN_CODE and attempt == 0:
                        token = client._get_token(rejected_token=token)
                        continue
                    break
                if response.status_code != 200 or type(payload.get("code")) is not int or payload["code"] != 0:
                    raise ValueError
                data = payload.get("data")
                if (not isinstance(data, dict) or data.get("trigger_security_conf_limit")
                        or not isinstance(data.get("items"), list) or type(data.get("has_more")) is not bool):
                    raise ValueError
                for member in data["items"]:
                    uid = member.get("member_id") if isinstance(member, dict) else None
                    if (isinstance(uid, str) and _OPEN_ID.fullmatch(uid)
                            and member.get("member_id_type", "open_id") == "open_id"):
                        members[uid] = _label(member.get("name"), uid)
                if not data["has_more"]:
                    return members
                cursor = data.get("page_token")
                if not isinstance(cursor, str) or not cursor.strip() or cursor in seen_tokens:
                    raise ValueError
                seen_tokens.add(cursor)
                params["page_token"] = cursor
    except (FeishuAppError, httpx.HTTPError, ValueError, OSError):
        raise MappingError(502, _MEMBERS_ERROR) from None
    raise MappingError(502, _MEMBERS_ERROR)


def get_store_members(settings, sid):
    sid = _store_key(sid)
    _, routing, revision = _routing_snapshot(Path(settings.feishu_routing_file))
    _, _, chat, group, fallback_group = _store_route(settings, routing, sid)
    roster = _members(settings, chat)
    members = [{"id": uid, "name": name} for uid, name in
               sorted(roster.items(), key=lambda item: (item[1].casefold(), item[0]))]
    return {"members": members, "revision": revision, "group": group,
            "fallback_enabled": fallback_group is not None, "fallback_group": fallback_group}


@contextmanager
def _mapping_lock(path: Path):
    lock = path.with_name(path.name + ".write.lock")
    if lock.is_symlink():
        raise OSError("Unsafe routing lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Unsafe routing lock")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
        except OSError:
            raise MappingError(409, _CONFLICT) from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def _replace_mapping(path, expected, payload, sid, stamp):
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".jx-routing-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        # Keep a recoverable copy of the entire routing document. The previous
        # bytes are preserved verbatim with restrictive permissions.
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=path.name + ".before-console-" + stamp + "-" + sid + "-",
                                         suffix=".json", delete=False) as backup:
            backup.write(expected)
            backup.flush()
            os.fsync(backup.fileno())
        if path.is_symlink() or path.read_bytes() != expected:
            raise MappingError(409, _CONFLICT)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_owner_names(settings):
    """Fill display names of already-mapped owners from each store group's roster.

    Only ``owner_display_names`` changes. Recipients, groups and verification
    fields are untouched, so no one gains or loses a notification.
    """
    if getattr(settings, "feishu_transport", "webhook") != "app":
        raise MappingError(503, "同步姓名需要配置飞书自建应用。")
    path = Path(settings.feishu_routing_file)
    raw, routing, _ = _routing_snapshot(path)
    groups, stores = routing.get("groups", {}), routing.get("stores", {})
    rosters, updated, unresolved = {}, 0, 0
    for sid, entry in stores.items():
        if not isinstance(entry, dict) or not entry.get("open_ids"):
            continue
        group = groups.get(entry.get("chat_id") or routing["default_chat_id"], {})
        chat = group.get("chat_id")
        if not _valid_chat(chat):
            unresolved += len(entry["open_ids"])
            continue
        if chat not in rosters:
            rosters[chat] = _members(settings, chat)
        roster = rosters[chat]
        previous = entry.get("owner_display_names")
        previous = previous if isinstance(previous, dict) else {}
        names = {}
        for uid in dict.fromkeys(entry["open_ids"]):
            if uid in roster:
                names[uid] = _label(roster[uid], uid)
            else:
                unresolved += 1
                if isinstance(previous.get(uid), str):
                    names[uid] = previous[uid]
        if names != previous:
            entry["owner_display_names"] = names
            updated += 1
    result = {"updated_store_count": updated, "unresolved_owner_count": unresolved}
    if not updated:
        return {**result, "revision": hashlib.sha256(raw).hexdigest()}
    payload = (json.dumps(routing, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with _mapping_lock(path):
            if path.is_symlink() or path.read_bytes() != raw:
                raise MappingError(409, _CONFLICT)
            now = datetime.now(timezone.utc)
            _replace_mapping(path, raw, payload, "owner-names", now.strftime("%Y%m%dT%H%M%SZ"))
    except OSError:
        raise MappingError(503, "保存飞书映射失败，请核对运行目录权限。") from None
    _LOG.info("Owner display names synchronized stores=%s at=%s", updated, now.isoformat())
    return {**result, "revision": hashlib.sha256(payload).hexdigest()}


def save_store_mapping(settings, sid, open_ids, revision):
    """Verify the requested members before changing the selected store entry."""
    sid = _store_key(sid)
    if (not isinstance(open_ids, list) or len(open_ids) > 100
            or any(not isinstance(uid, str) or not _OPEN_ID.fullmatch(uid) for uid in open_ids)):
        raise MappingError(400, "请选择有效的飞书群成员。")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise MappingError(400, "映射版本无效，请刷新后再保存。")
    ids = list(dict.fromkeys(open_ids))
    path = Path(settings.feishu_routing_file)
    raw, routing, current = _routing_snapshot(path)
    if revision != current:
        raise MappingError(409, _CONFLICT)
    route, alias, chat, _, fallback_group = _store_route(settings, routing, sid)
    if not ids and fallback_group is None:
        raise MappingError(400, "清空负责人前需启用并核实日报群兜底。")
    verified_app = scoped_app(settings)
    roster = (_members(settings, chat, app_id=verified_app) if verified_app else _members(settings, chat)) if ids else {}
    if any(uid not in roster for uid in ids):
        raise MappingError(400, "有选中的人员已不在此群中，请刷新群成员后再选择。")
    updated = dict(route)
    updated["open_ids"] = ids
    updated["owner_display_names"] = {uid: roster[uid] for uid in ids}
    scoped = scoped_routing_enabled(settings)
    if ids and verified_app:
        updated["hourly_verified_operator_app_id"] = verified_app
    else:
        updated.pop("hourly_verified_operator_app_id", None)
    if (scoped or "hourly_operator_generation" in route) and (ids != route.get("open_ids", [])
            or updated.get("hourly_verified_operator_app_id") != route.get("hourly_verified_operator_app_id")):
        updated["hourly_operator_generation"] = uuid4().hex
    if not route.get("chat_id") and alias != routing["default_chat_id"]:
        updated["chat_id"] = alias
    routing.setdefault("stores", {})[sid] = updated
    group_changed = False
    if ids and (scoped or "verified_member_open_ids" in routing["groups"][alias]):
        group = routing["groups"][alias]
        group_changed = (group.get("verified_member_open_ids") != sorted(roster)
                         or group.get("hourly_verified_roster_app_id") != verified_app)
        group["verified_member_open_ids"] = sorted(roster)
        if verified_app:
            group["hourly_verified_roster_app_id"] = verified_app
        else:
            group.pop("hourly_verified_roster_app_id", None)
    payload = (json.dumps(routing, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with _mapping_lock(path):
            if path.is_symlink() or path.read_bytes() != raw:
                raise MappingError(409, _CONFLICT)
            if updated == route and not group_changed:
                return _mapping(settings, routing, sid, revision)
            now = datetime.now(timezone.utc)
            _replace_mapping(path, raw, payload, sid, now.strftime("%Y%m%dT%H%M%SZ"))
    except OSError:
        raise MappingError(503, "保存飞书映射失败，请核对运行目录权限。") from None
    _LOG.info("Store owner mapping updated store=%s at=%s", sid, now.isoformat())
    return _mapping(settings, routing, sid, hashlib.sha256(payload).hexdigest())
