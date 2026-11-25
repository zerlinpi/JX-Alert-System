"""Owner-console Feishu operations: report group, group roster, 总负责人 and 广告负责人.

Official contracts:
https://open.feishu.cn/document/server-docs/group/chat/list
https://open.feishu.cn/document/server-docs/group/chat/get-2
https://open.feishu.cn/document/server-docs/group/chat-member/get
Every write keeps a verbatim 0600 backup of the previous routing document.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import uuid

import httpx

from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.notifications.routing import CHAT_ID, OPEN_ID, ApplicationRoutingError, report_group, validate_routing
from jx_alert.reports.recipients import AD_LEAD, COMPANY_LEAD
from jx_alert.sparkx.stores import clean_name
from jx_alert.storage.job_lock import _try_file_lock, _unlock_file

_LOG = logging.getLogger(__name__)
_CONFLICT = "通知配置已更新，请刷新后重新保存。"
_MEMBERS_ERROR = "无法读取飞书群成员。请确认自建应用已加入该群并获准读取群成员。"
_GROUP_ERROR = "无法读取机器人所在群。请确认已开启查看群信息权限，并将机器人加入目标群后刷新。"


class MappingError(RuntimeError):
    """Safe outcome for an authenticated management route."""

    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _require_app(settings):
    if getattr(settings, "feishu_transport", "app") != "app":
        raise MappingError(503, "需要配置飞书自建应用（FEISHU_TRANSPORT=app）。")


def snapshot(settings):
    path = Path(settings.feishu_routing_file)
    try:
        if path.is_symlink():
            raise OSError
        raw = path.read_bytes() if path.exists() else b"{}"
        routing = validate_routing(json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_fields))
    except (OSError, ValueError, RuntimeError):
        raise MappingError(503, "飞书路由配置不可用，请核对群配置。") from None
    return raw, routing, hashlib.sha256(raw).hexdigest()


def _group(settings, routing):
    try:
        return report_group(settings, routing)
    except ApplicationRoutingError:
        raise MappingError(503, "尚未配置通知群（FEISHU_DAILY_REPORT_CHAT_ID 对应的群），请先在「通知群」中设置。") from None


def group_label(group, alias):
    return clean_name(group.get("display_name") or group.get("name"), 100) or alias


@contextmanager
def _lock(path):
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


def write_routing(settings, expected_raw, routing, label):
    """Replace the routing document if unchanged since `expected_raw`; keep a verbatim backup."""
    path = Path(settings.feishu_routing_file)
    payload = (json.dumps(routing, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        with _lock(path):
            current = path.read_bytes() if path.exists() else b"{}"
            if path.is_symlink() or current != expected_raw:
                raise MappingError(409, _CONFLICT)
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".jx-routing-", suffix=".tmp",
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, mode)
                if path.exists():
                    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                                     prefix=f"{path.name}.before-console-{stamp}-{label}-",
                                                     suffix=".json", delete=False) as backup:
                        backup.write(expected_raw)
                        backup.flush()
                        os.fsync(backup.fileno())
                    os.chmod(backup.name, 0o600)
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except OSError:
        raise MappingError(503, "保存飞书配置失败，请核对运行目录权限。") from None
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------- Feishu reads

def _get(http, client, path, params=None):
    token = client._get_token()
    for attempt in range(2):
        response = http.get(client.base_url + path, params=params, headers={"Authorization": "Bearer " + token})
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


def members(settings, chat):
    """{open_id: name} of one group, fetched live."""
    _require_app(settings)
    try:
        client = FeishuAppClient.from_settings(settings)
        params = {"member_id_type": "open_id", "page_size": 100}
        found, seen = {}, set()
        with httpx.Client(timeout=client.timeout, follow_redirects=False) as http:
            for _ in range(100):
                data = _get(http, client, f"/open-apis/im/v1/chats/{chat}/members", params)
                if (data.get("trigger_security_conf_limit") or not isinstance(data.get("items"), list)
                        or type(data.get("has_more")) is not bool):
                    raise ValueError
                for member in data["items"]:
                    uid = member.get("member_id") if isinstance(member, dict) else None
                    if isinstance(uid, str) and OPEN_ID.fullmatch(uid) and member.get("member_id_type", "open_id") == "open_id":
                        found[uid] = clean_name(member.get("name"), 100) or uid
                if not data["has_more"]:
                    return found
                cursor = data.get("page_token")
                if not isinstance(cursor, str) or not cursor.strip() or cursor in seen:
                    raise ValueError
                seen.add(cursor)
                params["page_token"] = cursor
    except (FeishuAppError, httpx.HTTPError, ValueError, OSError):
        raise MappingError(502, _MEMBERS_ERROR) from None
    raise MappingError(502, _MEMBERS_ERROR)


def joined_chats(settings, *, selected=None):
    """Groups this bot has joined (never the wider directory or p2p chats)."""
    _require_app(settings)
    try:
        client = FeishuAppClient.from_settings(settings)
        with httpx.Client(timeout=client.timeout, follow_redirects=False) as http:
            params, chats, seen = {"page_size": 100, "sort_type": "ByCreateTimeAsc"}, {}, set()
            for _ in range(100):
                data = _get(http, client, "/open-apis/im/v1/chats", params)
                if not isinstance(data.get("items"), list) or type(data.get("has_more")) is not bool:
                    raise ValueError
                for item in data["items"]:
                    chat_id = item.get("chat_id") if isinstance(item, dict) else None
                    if not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id):
                        raise ValueError
                    chats[chat_id] = clean_name(item.get("name"), 100) or "未命名群"
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
                    raise MappingError(400, "目标群不在机器人已加入的群列表中，请先添加机器人并刷新。")
                detail = _get(http, client, "/open-apis/im/v1/chats/" + selected)
                if detail.get("chat_mode") not in {"group", "topic"}:
                    raise MappingError(400, "通知目标必须是群聊，不能使用单聊。")
                chats[selected] = clean_name(detail.get("name"), 100) or chats[selected]
            return chats
    except MappingError:
        raise
    except (FeishuAppError, httpx.HTTPError, ValueError, OSError):
        raise MappingError(502, _GROUP_ERROR) from None


# ---------------------------------------------------------------- report group

def current_group(settings):
    _, routing, revision = snapshot(settings)
    alias, group = _group(settings, routing)
    return {"chat_id": group["chat_id"], "name": group_label(group, alias), "alias": alias, "revision": revision,
            "verified_members": len(group.get("verified_member_open_ids") or []) or None}


def list_groups(settings):
    current = current_group(settings)
    chats = joined_chats(settings)
    return {**current, "chats": [{"id": uid, "name": name} for uid, name in
                                  sorted(chats.items(), key=lambda item: (item[1].casefold(), item[0]))]}


def save_group(settings, chat_id, revision, confirmed):
    """Point the report-group alias at another joined group; older queued messages stop."""
    if confirmed is not True:
        raise MappingError(400, "请先确认通知群替换范围。")
    if not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id) or not isinstance(revision, str):
        raise MappingError(400, "请选择有效的通知群。")
    raw, routing, current = snapshot(settings)
    if current != revision:
        raise MappingError(409, _CONFLICT)
    alias, group = _group(settings, routing)
    chats = joined_chats(settings, selected=chat_id)
    roster = members(settings, chat_id)
    updated = {**group, "chat_id": chat_id, "display_name": chats[chat_id],
               "admin_open_ids": [uid for uid in group.get("admin_open_ids", []) if uid in roster],
               "verified_member_open_ids": sorted(roster)}
    if chat_id != group["chat_id"]:
        now = datetime.now(timezone.utc).isoformat()
        updated.update(notifications_changed_at=now,
                       retired_chat_ids={**group.get("retired_chat_ids", {}), group["chat_id"]: now})
    if updated == group:
        return current_group(settings)
    routing["groups"][alias] = updated
    write_routing(settings, raw, routing, "notification-group")
    _LOG.info("Notification group selection saved")
    return current_group(settings)


def group_members(settings, *, refresh=False):
    """Live roster of the report group; optionally store it as the verified roster."""
    raw, routing, _ = snapshot(settings)
    alias, group = _group(settings, routing)
    roster = members(settings, group["chat_id"])
    if refresh:
        refresh_roster(settings, roster)
    return [{"id": uid, "name": name} for uid, name in sorted(roster.items(), key=lambda item: (item[1].casefold(), item[0]))]


def refresh_roster(settings, roster):
    """Store the live roster as verified_member_open_ids (one retry on a concurrent edit)."""
    for _ in range(2):
        raw, routing, _ = snapshot(settings)
        alias, group = _group(settings, routing)
        if group.get("verified_member_open_ids") == sorted(roster):
            return False
        routing["groups"][alias] = {**group, "verified_member_open_ids": sorted(roster)}
        try:
            write_routing(settings, raw, routing, "group-roster")
            return True
        except MappingError as exc:
            if exc.status_code != 409:
                raise
    raise MappingError(409, _CONFLICT)


def verified_people(settings, open_ids):
    """[{open_id, name}] for ids that are current report-group members (else 400)."""
    if (not isinstance(open_ids, list) or len(open_ids) > 10
            or any(not isinstance(uid, str) or not OPEN_ID.fullmatch(uid) for uid in open_ids)):
        raise MappingError(400, "请选择有效的飞书群成员（每家店最多 10 位运营）。")
    ids = list(dict.fromkeys(open_ids))
    if not ids:
        return []
    _, routing, _ = snapshot(settings)
    _, group = _group(settings, routing)
    roster = members(settings, group["chat_id"])
    if any(uid not in roster for uid in ids):
        raise MappingError(400, "有选中的人员已不在通知群中，请刷新群成员后再选择。")
    refresh_roster(settings, roster)
    return [{"open_id": uid, "name": roster[uid]} for uid in ids]


# ---------------------------------------------------------------- leads

MAX_LEADS = 10


def _stored_people(section, first):
    """{open_id: stored row} of a lead section: the first person's row and ``more_recipients``."""
    rows = [*first, *(section.get("more_recipients") or [])] if isinstance(section, dict) else []
    return {row["open_id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("open_id"), str)}


def _company_rows(routing):
    binding = routing.get(COMPANY_LEAD)
    return _stored_people(binding, [binding] if isinstance(binding, dict) else [])


def _ad_rows(routing):
    section = routing.get(AD_LEAD)
    rows = section.get("recipients") if isinstance(section, dict) else None
    return _stored_people(section, rows if isinstance(rows, list) else [])


def _lead_entries(routing, kind):
    rows = _company_rows(routing) if kind == "company" else _ad_rows(routing)
    return [{"id": uid, "name": clean_name(row.get("name"), 100) or uid} for uid, row in rows.items()][:MAX_LEADS]


def leads(settings):
    """Both lead lists for the console: {"company": [{id, name}], "ad": [...]} (empty when not set)."""
    _, routing, revision = snapshot(settings)
    try:
        alias, group = report_group(settings, routing)
        group_name = group_label(group, alias)
    except ApplicationRoutingError:
        group_name = None
    return {"revision": revision, "group": group_name, "company": _lead_entries(routing, "company"),
            "ad": _lead_entries(routing, "ad"), "max": MAX_LEADS}


def save_lead(settings, kind, open_ids, revision):
    """kind=company: 总负责人 (daily report); kind=ad: 广告负责人 (review and hourly); up to 10 people each.

    The first person is stored where earlier releases read the single lead, the others in
    ``more_recipients``; a person who stays keeps their generation (queued messages stay valid).
    """
    if kind not in {"company", "ad"} or not isinstance(open_ids, list) or len(open_ids) > MAX_LEADS:
        raise MappingError(400, f"每类负责人最多选择 {MAX_LEADS} 位。")
    raw, routing, current = snapshot(settings)
    if current != revision:
        raise MappingError(409, _CONFLICT)
    people = verified_people(settings, open_ids)
    raw, routing, current = snapshot(settings)  # The roster refresh may have rewritten the file.
    now = datetime.now(timezone.utc)
    if kind == "company":
        if not people:
            routing.pop(COMPANY_LEAD, None)
        else:
            try:
                app_id = FeishuAppClient.configured_app_id(settings)
            except (FeishuAppError, OSError, TypeError, ValueError):
                raise MappingError(503, "飞书自建应用凭据不可用，暂不能绑定私聊接收人。") from None
            old = routing.get(COMPANY_LEAD) if isinstance(routing.get(COMPANY_LEAD), dict) else {}
            kept = _company_rows(routing) if old.get("app_id") == app_id else {}
            stamp = (now - timedelta(seconds=5)).isoformat()
            rows = []
            for person in people:
                previous = kept.get(person["open_id"], {})
                same = isinstance(previous.get("generation"), str)
                rows.append({"open_id": person["open_id"], "name": person["name"],
                             "generation": previous["generation"] if same else uuid.uuid4().hex,
                             "verified_at": previous.get("verified_at") if same else stamp})
            routing[COMPANY_LEAD] = {**rows[0], "app_id": app_id,
                                     **({"more_recipients": rows[1:]} if len(rows) > 1 else {})}
    else:
        old = _ad_rows(routing)
        rows = [{"open_id": person["open_id"], "name": person["name"],
                 "generation": old.get(person["open_id"], {}).get("generation") or uuid.uuid4().hex}
                for person in people]
        routing[AD_LEAD] = {"recipients": rows[:1], **({"more_recipients": rows[1:]} if len(rows) > 1 else {}),
                            "updated_at": now.isoformat()}
    write_routing(settings, raw, routing, f"{kind}-lead")
    return leads(settings)
