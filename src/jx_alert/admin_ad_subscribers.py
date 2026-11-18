"""Owner edits to the single private advertising lead in the routing registry."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from jx_alert import admin_groups, admin_mapping
from jx_alert.notifications.recipient_policy import SECTION, subscriber_entries


def _snapshot(settings):
    raw, routing, revision, alias, group = admin_groups._snapshot(settings)
    entries = subscriber_entries(routing)
    if entries is None:
        raise admin_mapping.MappingError(503, '广告异常私聊配置无效，请由管理员核对。')
    return raw, routing, revision, alias, group, entries


def _view(group, alias, revision, entries):
    return {'revision': revision, 'group': admin_mapping._group_label(group, alias),
            'recipients': [{'id': row['open_id'], 'name': row['name']} for row in entries]}


def get_ad_subscribers(settings, *, include_members=False):
    _, _, revision, alias, group, entries = _snapshot(settings)
    result = _view(group, alias, revision, entries)
    if include_members:
        roster = admin_mapping._members(settings, group['chat_id'])
        result['members'] = [{'id': uid, 'name': name} for uid, name in
            sorted(roster.items(), key=lambda item: (item[1].casefold(), item[0]))]
    return result


def save_ad_subscribers(settings, open_ids, revision):
    if (not isinstance(open_ids, list) or len(open_ids) > 100
            or any(not isinstance(uid, str) or not admin_mapping._OPEN_ID.fullmatch(uid) for uid in open_ids)
            or not isinstance(revision, str) or not admin_mapping._REVISION.fullmatch(revision)):
        raise admin_mapping.MappingError(400, '请选择有效的接收人员并刷新配置版本。')
    ids = list(dict.fromkeys(open_ids))
    if len(ids) > 1:
        raise admin_mapping.MappingError(400, '只能设置一位广告总负责人。')
    raw, routing, current, alias, group, entries = _snapshot(settings)
    if current != revision:
        raise admin_mapping.MappingError(409, '通知配置已更新，请刷新后重新选择。')
    roster = admin_mapping._members(settings, group['chat_id']) if ids else {}
    if any(uid not in roster for uid in ids):
        raise admin_mapping.MappingError(400, '有选中人员不在当前通知群，请刷新群成员。')
    old = {row['open_id']: row for row in entries} if len(entries) == 1 else {}
    recipients = [{'open_id': uid, 'name': admin_mapping._label(roster[uid], uid),
                   'generation': old[uid]['generation'] if uid in old else uuid.uuid4().hex} for uid in ids]
    if recipients == entries:
        return _view(group, alias, current, entries)
    stamp = datetime.now(timezone.utc)
    routing[SECTION] = {'recipients': recipients, 'updated_at': stamp.isoformat()}
    payload = (json.dumps(routing, ensure_ascii=False, indent=2) + '\n').encode()
    path = Path(settings.feishu_routing_file)
    try:
        with admin_mapping._mapping_lock(path):
            if path.is_symlink() or path.read_bytes() != raw:
                raise admin_mapping.MappingError(409, '通知配置已更新，请刷新后重新选择。')
            admin_mapping._replace_mapping(path, raw, payload, 'ad-subscribers', stamp.strftime('%Y%m%dT%H%M%SZ'))
    except OSError:
        raise admin_mapping.MappingError(503, '广告异常私聊配置保存失败，请稍后重试。') from None
    return _view(group, alias, hashlib.sha256(payload).hexdigest(), recipients)
