"""Independent, persistent collection and notification switches per store.

The explicit collection scope is unchanged: disabled stores remain queryable.
Missing files retain existing behavior; malformed controls never enable work.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile

from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.storage.job_lock import _try_file_lock, _unlock_file


_LOG = logging.getLogger(__name__)
_FIELDS = {"collection_enabled", "notifications_enabled"}
_UNAVAILABLE = "店铺开关配置不可用，请检查运行目录中的配置文件。"
_CONFLICT = "店铺开关已有更新，请刷新后再保存。"


class StoreControlError(RuntimeError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _sid(value):
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,99}", str(value)):
        raise StoreControlError(400, "店铺编号无效。")
    return str(value)


def _path(settings):
    return Path(getattr(settings, "store_controls_file", "/app/data/store-controls.json"))


def _snapshot(path):
    try:
        if path.is_symlink():
            raise ValueError
        if not path.exists():
            raw, data = b"", {"version": 1, "stores": {}}
        else:
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError
            raw = path.read_bytes()
            data = json.loads(raw, object_pairs_hook=unique_json_fields)
        if (not isinstance(data, dict) or set(data) != {"version", "stores"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["stores"], dict)):
            raise ValueError
        for sid, row in data["stores"].items():
            _sid(sid)
            if (not isinstance(row, dict) or set(row) - {"notification_cutoff_at"} != _FIELDS | {"updated_at"}
                    or any(type(row[key]) is not bool for key in _FIELDS)):
                raise ValueError
            stamp = datetime.fromisoformat(row["updated_at"])
            if stamp.tzinfo is None:
                raise ValueError
            cutoff = row.get("notification_cutoff_at")
            if cutoff is not None and datetime.fromisoformat(cutoff).tzinfo is None:
                raise ValueError
    except (OSError, ValueError, TypeError, RecursionError, StoreControlError):
        raise StoreControlError(503, _UNAVAILABLE) from None
    return raw, data, hashlib.sha256(raw).hexdigest()


def _view(data, sid, revision):
    row = data["stores"].get(sid, {})
    return {"collection_enabled": row.get("collection_enabled", True),
            "notifications_enabled": row.get("notifications_enabled", True),
            "updated_at": row.get("updated_at"), "revision": revision}


def store_control_snapshots(settings, sids):
    _, data, revision = _snapshot(_path(settings))
    return {str(sid): _view(data, _sid(sid), revision) for sid in sids}


def get_store_controls(settings, sid):
    sid = _sid(sid)
    return store_control_snapshots(settings, [sid])[sid]


def collection_enabled(settings, sid):
    return collection_states(settings, [sid])[str(sid)]


def collection_states(settings, sids):
    _, data, _ = _snapshot(_path(settings))
    return {str(sid): data["stores"].get(str(sid), {}).get("collection_enabled", True) for sid in sids}


def notification_states(settings, sids=None):
    """Read one consistent view, including the latest pause for queued work."""
    _, data, _ = _snapshot(_path(settings))
    states = {}
    for sid in data["stores"] if sids is None else sids:
        row = data["stores"].get(str(sid), {})
        stamp = row.get("notification_cutoff_at")
        states[str(sid)] = (row.get("notifications_enabled", True),
                            datetime.fromisoformat(stamp) if stamp else None)
    return states


def notifications_enabled(settings, sid):
    return notification_states(settings, [sid])[str(sid)][0]


def require_notifications(settings, sid):
    if not notifications_enabled(settings, sid):
        raise StoreControlError(409, "该店铺已关闭飞书通知，请先在运营台开启。")


@contextmanager
def _lock(path):
    target = path.with_name(path.name + ".lock")
    if target.is_symlink():
        raise StoreControlError(503, _UNAVAILABLE)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
            acquired = True
        except OSError:
            raise StoreControlError(409, _CONFLICT) from None
        yield
    finally:
        if acquired:
            _unlock_file(fd)
        os.close(fd)


def save_store_controls(settings, sid, collection_enabled, notifications_enabled, revision):
    sid = _sid(sid)
    if type(collection_enabled) is not bool or type(notifications_enabled) is not bool:
        raise StoreControlError(400, "采集和通知开关必须分别为开启或关闭。")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise StoreControlError(400, "店铺开关版本无效，请刷新后再保存。")
    path = _path(settings)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock(path):
            raw, data, current = _snapshot(path)
            if current != revision:
                raise StoreControlError(409, _CONFLICT)
            previous = _view(data, sid, current)
            if (previous["collection_enabled"] == collection_enabled
                    and previous["notifications_enabled"] == notifications_enabled):
                return previous
            now = datetime.now(timezone.utc)
            cutoff = data["stores"].get(sid, {}).get("notification_cutoff_at")
            if notifications_enabled != previous["notifications_enabled"]:
                cutoff = now.isoformat()
            data["stores"][sid] = {"collection_enabled": collection_enabled,
                                   "notifications_enabled": notifications_enabled,
                                   "notification_cutoff_at": cutoff,
                                   "updated_at": now.isoformat()}
            payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".jx-controls-",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                    prefix=path.name + ".before-switch-" + now.strftime("%Y%m%dT%H%M%SZ") + "-" + sid + "-",
                    suffix=".json", delete=False) as backup:
                backup.write(raw or b'{"version":1,"stores":{}}\n')
                backup.flush()
                os.fsync(backup.fileno())
                os.chmod(backup.name, 0o600)
            if _snapshot(path)[0] != raw:
                raise StoreControlError(409, _CONFLICT)
            os.replace(temporary, path)
    except OSError:
        raise StoreControlError(503, "无法保存店铺开关，请检查运行目录权限。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _LOG.info("Store controls changed store=%s collection=%s notifications=%s at=%s",
              sid, collection_enabled, notifications_enabled, now.isoformat())
    return _view(data, sid, hashlib.sha256(payload).hexdigest())
