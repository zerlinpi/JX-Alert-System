"""Private, versioned Lingxing connections; public projections never contain secrets.

The owner rotates credentials through the HTTPS console. Readers reload this
file at each collection boundary; neither environment files nor old credential
versions are rewritten. Expiry is an owner-declared replacement date, not a
claim about the upstream provider's token lifetime.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from zoneinfo import ZoneInfo

from jx_alert.storage.job_lock import _try_file_lock, _unlock_file


API_ORIGIN = "https://openapi.lingxing.com"
MCP_URL = "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp"
MCP_ORIGIN = "https://openmcp.lingxing.com"
_REVISION = re.compile(r"[0-9a-f]{64}")
_KINDS = {"api", "mcp"}
_EMPTY_REVISION = "0" * 64
_MAX_BYTES = 512 * 1024


class SourceCredentialsError(RuntimeError):
    def __init__(self, detail="领星数据源配置不可用，请在后台核对。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


SourceCredentialError = SourceCredentialsError


def _path(settings):
    return Path(getattr(settings, "lingxing_sources_file", "/app/data/secrets/lingxing-sources.json"))


def _private_path(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise SourceCredentialsError()
    if path.exists():
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BYTES:
            raise SourceCredentialsError()
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SourceCredentialsError("数据源私有文件权限异常，请由管理员核对。")


def _read(settings):
    path = _path(settings)
    _private_path(path)
    if not path.exists():
        return {"version": 1, "revision": _EMPTY_REVISION, "sources": {}, "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or not _REVISION.fullmatch(data.get("revision", ""))
                or not isinstance(data.get("sources"), dict) or set(data["sources"]) - _KINDS
                or not isinstance(data.get("events"), list)):
            raise ValueError
        for kind, entry in data["sources"].items():
            if (not isinstance(entry, dict) or not isinstance(entry.get("credentials"), dict)
                    or not re.fullmatch(r"[0-9a-f]{32}", entry.get("version_id", ""))
                    or not isinstance(entry.get("updated_at"), str)):
                raise ValueError
            _expiry(entry.get("expires_on"))
        return data
    except (ValueError, TypeError, OSError):
        raise SourceCredentialsError() from None


def _expiry(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SourceCredentialsError("请选择有效的更换日期。", 400)
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError:
        raise SourceCredentialsError("请选择有效的更换日期。", 400) from None


def _today(now=None):
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Credential clocks must include a timezone")
    return clock.astimezone(ZoneInfo("America/Los_Angeles")).date()


def _expired(entry, now=None):
    expires = _expiry(entry.get("expires_on"))
    return expires is not None and expires < _today(now)


def _text(value, *, maximum=4096):
    if (not isinstance(value, str) or len(value) > maximum
            or any(c.isspace() or not c.isprintable() for c in value)):
        raise SourceCredentialsError("凭据格式无效，请重新填写。", 400)
    return value


def _api_credentials(supplied, previous):
    allowed = {"app_id", "app_secret", "app_key", "access_token"}
    if set(supplied) - allowed:
        raise SourceCredentialsError("接口配置包含不支持的字段。", 400)
    values = {key: _text(value, maximum=128 if key in {"app_id", "app_key"} else 4096)
              for key, value in supplied.items()}
    changed_identity = bool(values.get("app_id") and values["app_id"] != previous.get("app_id"))
    if changed_identity and not (values.get("app_secret") or values.get("access_token")):
        raise SourceCredentialsError("更换App ID时，请同时填写该身份的App Secret或Access Token。", 400)
    # Secrets and tokens belong to an application identity. They must never be
    # inherited merely because the user left the new identity's fields blank.
    result = {} if changed_identity else dict(previous)
    result.update({key: value for key, value in values.items() if value})
    if changed_identity and not values.get("app_key"):
        result["app_key"] = values["app_id"]
    if values.get("app_secret") and not values.get("access_token"):
        # Explicitly switching back to managed AppSecret authentication must
        # not keep an older static token, which otherwise takes precedence.
        result["access_token"] = ""
    if not result.get("app_id") or not (result.get("app_secret") or result.get("access_token")):
        raise SourceCredentialsError("请填写App ID，并提供App Secret或Access Token。", 400)
    result["app_key"] = result.get("app_key") or result["app_id"]
    return {key: result.get(key, "") for key in allowed}


def _mcp_credentials(supplied, previous):
    if set(supplied) - {"url", "key"}:
        raise SourceCredentialsError("MCP配置包含不支持的字段。", 400)
    result = dict(previous)
    result.update({key: _text(value) for key, value in supplied.items() if value != ""})
    if result.get("url") != MCP_URL:
        raise SourceCredentialsError("请使用领星官方MCP地址。", 400)
    key = result.get("key")
    if not isinstance(key, str) or len(_text(key)) < 16:
        raise SourceCredentialsError("请填写有效的领星MCP Key。", 400)
    return {"url": MCP_URL, "key": key}


def _legacy_api(settings):
    return {field: getattr(settings, "lingxing_" + field, "") or ""
            for field in ("app_id", "app_secret", "app_key", "access_token")}


def _legacy_mcp(settings):
    path = Path(getattr(settings, "lingxing_business_credentials_file", ""))
    if path.is_file():
        _private_path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return _mcp_credentials(data, {})
        except (ValueError, OSError):
            raise SourceCredentialsError() from None
    url = getattr(settings, "lingxing_mcp_url", "")
    secret = getattr(settings, "lingxing_mcp_key", "")
    key = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if url or key:
        return _mcp_credentials({"url": url, "key": key}, {})
    return None


def mcp_credentials(settings, *, now=None):
    entry = _read(settings)["sources"].get("mcp")
    if entry is None:
        value = _legacy_mcp(settings)
        return (value["url"], value["key"]) if value else None
    if _expired(entry, now):
        raise SourceCredentialsError("领星MCP已超过登记的更换日期，请在后台更新。")
    value = _mcp_credentials(entry["credentials"], {})
    return value["url"], value["key"]


def effective_api_settings(settings, *, now=None):
    entry = _read(settings)["sources"].get("api")
    if entry is None:
        return settings
    if _expired(entry, now):
        raise SourceCredentialsError("领星接口已超过登记的更换日期，请在后台更新。")
    values = _api_credentials(entry["credentials"], {})
    updates = {"lingxing_" + key: value for key, value in values.items()}
    updates.update(lingxing_base_url=API_ORIGIN, lingxing_refresh_token="",
        lingxing_token_store_path=str(_path(settings).with_name("lingxing-token-api-" + entry["version_id"] + ".json")))
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    from copy import copy
    result = copy(settings)
    result.__dict__.update(updates)
    return result


def source_configuration(settings, *, now=None):
    data = _read(settings)
    result = {"revision": data["revision"], "events": []}
    for kind in ("api", "mcp"):
        entry = data["sources"].get(kind)
        configured = False
        legacy_invalid = False
        if entry:
            configured = True
        elif kind == "api":
            legacy = _legacy_api(settings)
            configured = bool(legacy["app_id"] and (legacy["app_secret"] or legacy["access_token"]))
        else:
            try:
                configured = _legacy_mcp(settings) is not None
            except SourceCredentialsError:
                configured, legacy_invalid = True, True
        expires = _expiry(entry.get("expires_on")) if entry else None
        status = ("INVALID" if legacy_invalid else "EXPIRED" if entry and _expired(entry, now) else
                  "EXPIRING" if expires and expires <= _today(now) + timedelta(days=7) else
                  "CONFIGURED" if configured else "NOT_CONFIGURED")
        result[kind] = {"configured": configured, "origin": (API_ORIGIN if kind == "api" else MCP_ORIGIN),
            "updated_at": entry.get("updated_at") if entry else None,
            "expires_on": expires.isoformat() if expires else None, "status": status,
            "status_label": {"INVALID": "配置需更新", "EXPIRED": "已到更换日期", "EXPIRING": "七日内需更换",
                "CONFIGURED": "已配置，连接结果以采集为准", "NOT_CONFIGURED": "未配置"}[status]}
    for event in data["events"][-100:]:
        if isinstance(event, dict) and event.get("kind") in _KINDS and event.get("action") in {"ADD", "ROTATE"}:
            result["events"].append({key: event.get(key) for key in ("at", "kind", "action", "expires_on")})
    result["events"].reverse()
    return result


@contextmanager
def _locked(path):
    if path.parent.is_symlink():
        raise SourceCredentialsError()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock = path.with_name(path.name + ".lock")
    if lock.is_symlink():
        raise SourceCredentialsError()
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceCredentialsError()
        os.chmod(lock, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
        except OSError:
            raise SourceCredentialsError("其他人正在更新数据源，请稍后重试。", 409) from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def save_source(settings, *, revision, kind, credentials, expires_on=None, now=None):
    if (not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            or not isinstance(kind, str) or kind not in _KINDS
            or not isinstance(credentials, dict) or not credentials):
        raise SourceCredentialsError("提交的数据源配置无效。", 400)
    clock = now or datetime.now(timezone.utc)
    expires = _expiry(expires_on)
    if expires is not None and expires < _today(clock):
        raise SourceCredentialsError("更换日期不能早于当前美国太平洋日期。", 400)
    path = _path(settings)
    with _locked(path):
        data = _read(settings)
        if data["revision"] != revision:
            raise SourceCredentialsError("数据源已更新，请刷新后重新填写。", 409)
        old = data["sources"].get(kind)
        if old:
            previous = old["credentials"]
        elif kind == "api":
            previous = _legacy_api(settings)
        else:
            try:
                previous = _legacy_mcp(settings) or {}
            except SourceCredentialsError:
                # A bad legacy file must not prevent replacing it with a full,
                # valid private UI configuration. Partial blanks cannot repair it.
                previous = {}
        values = _api_credentials(credentials, previous) if kind == "api" else _mcp_credentials(credentials, previous)
        stamp = clock.astimezone(timezone.utc).isoformat()
        action = "ROTATE" if old or (previous.get("app_id") if kind == "api" else previous) else "ADD"
        data["sources"][kind] = {"version_id": secrets.token_hex(16), "credentials": values,
            "updated_at": stamp, "expires_on": expires.isoformat() if expires else None}
        data["revision"] = secrets.token_hex(32)
        data["events"] = [*data["events"][-999:], {"at": stamp, "kind": kind, "action": action,
            "expires_on": expires.isoformat() if expires else None}]
        fd, temporary = tempfile.mkstemp(prefix=".lingxing-sources-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return source_configuration(settings, now=clock)
