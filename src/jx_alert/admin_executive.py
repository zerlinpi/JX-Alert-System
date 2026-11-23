"""Owner selection of the single private recipient of the 08:30 company digest."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid

from jx_alert import admin_groups, admin_mapping
from jx_alert.notifications import company_executive
from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError


def _snapshot(settings):
    raw, routing, revision, alias, group = admin_groups._snapshot(settings)
    binding = routing.get(company_executive.SECTION)
    if binding is not None and (not isinstance(binding, dict) or not isinstance(binding.get("open_id"), str)):
        raise admin_mapping.MappingError(503, "公司日报接收人配置无效，请由管理员核对。")
    return raw, routing, revision, alias, group, binding


def _view(settings, group, alias, revision, binding):
    recipient = None
    if binding:
        name = binding.get("name")
        recipient = {"id": binding["open_id"],
                     "name": name if isinstance(name, str) and name.strip() else binding["open_id"]}
    return {"revision": revision, "group": admin_mapping._group_label(group, alias),
            "recipient": recipient, "status": company_executive.digest_status(settings)}


def get_executive(settings, *, include_members=False):
    _, _, revision, alias, group, binding = _snapshot(settings)
    result = _view(settings, group, alias, revision, binding)
    if include_members:
        roster = admin_mapping._members(settings, group["chat_id"])
        result["members"] = [{"id": uid, "name": name} for uid, name in
                             sorted(roster.items(), key=lambda item: (item[1].casefold(), item[0]))]
    return result


def save_executive(settings, open_ids, revision):
    """Bind one verified report-group member to the configured Feishu application."""
    if (not isinstance(open_ids, list) or len(open_ids) > 1
            or any(not isinstance(uid, str) or not admin_mapping._OPEN_ID.fullmatch(uid) for uid in open_ids)
            or not isinstance(revision, str) or not admin_mapping._REVISION.fullmatch(revision)):
        raise admin_mapping.MappingError(400, "请选择一位公司日报总负责人，并刷新配置版本。")
    raw, routing, current, alias, group, binding = _snapshot(settings)
    if current != revision:
        raise admin_mapping.MappingError(409, "通知配置已更新，请刷新后重新选择。")
    if not open_ids:
        if binding is None:
            return _view(settings, group, alias, current, None)
        routing.pop(company_executive.SECTION, None)
        updated = None
    else:
        uid = open_ids[0]
        roster = admin_mapping._members(settings, group["chat_id"])
        if uid not in roster:
            raise admin_mapping.MappingError(400, "所选人员不在当前通知群，请刷新群成员。")
        try:
            app_id = FeishuAppClient.configured_app_id(settings)
        except (FeishuAppError, OSError, TypeError, ValueError):
            raise admin_mapping.MappingError(503, "飞书自建应用凭据不可用，暂不能绑定私聊接收人。") from None
        name = admin_mapping._label(roster[uid], uid)
        same = (binding is not None and binding.get("open_id") == uid and binding.get("app_id") == app_id)
        if same and binding.get("name") == name:
            return _view(settings, group, alias, current, binding)
        # A short backdate keeps the binding valid against small host clock skew.
        stamp = datetime.now(timezone.utc) - timedelta(seconds=5)
        updated = {"open_id": uid, "name": name, "app_id": app_id,
                   "generation": binding["generation"] if same else uuid.uuid4().hex,
                   "verified_at": binding["verified_at"] if same else stamp.isoformat()}
        if binding is not None and "store_ids" in binding:
            updated["store_ids"] = binding["store_ids"]
        routing[company_executive.SECTION] = updated
    payload = (json.dumps(routing, ensure_ascii=False, indent=2) + "\n").encode()
    path = Path(settings.feishu_routing_file)
    now = datetime.now(timezone.utc)
    try:
        with admin_mapping._mapping_lock(path):
            if path.is_symlink() or path.read_bytes() != raw:
                raise admin_mapping.MappingError(409, "通知配置已更新，请刷新后重新选择。")
            admin_mapping._replace_mapping(path, raw, payload, "company-digest", now.strftime("%Y%m%dT%H%M%SZ"))
    except OSError:
        raise admin_mapping.MappingError(503, "公司日报接收人保存失败，请稍后重试。") from None
    return _view(settings, group, alias, hashlib.sha256(payload).hexdigest(), updated)
