"""Owner-console SparkX MCP credentials, with the deployment environment as fallback.

The token is write-only: it is stored in a private 0600 file and never returned.
Only `get_user_authorized_context` is used to test it; no write tool exists here.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from types import SimpleNamespace

from pydantic import SecretStr

from jx_alert.storage.job_lock import _try_file_lock, _unlock_file

_EMPTY_REVISION = "0" * 64
_MAX_BYTES = 16 * 1024
_REVISION = re.compile(r"[0-9a-f]{64}")
_PROFILES = re.compile(r"(?:[1-9][0-9]{0,18})(?:,[1-9][0-9]{0,18}){0,199}")


class SparkXConfigError(RuntimeError):
    def __init__(self, detail="SparkX 私有配置无法读取，请联系管理员。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def _path(settings):
    name = getattr(settings, "sparkx_credentials_file", "")
    return Path(name) if name else None


def _secret(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else (value or "")


def _read(path):
    if path is None:
        return {}, _EMPTY_REVISION
    if path.is_symlink() or path.parent.is_symlink():
        raise SparkXConfigError()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {}, _EMPTY_REVISION
    except OSError:
        raise SparkXConfigError() from None
    try:
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            raw = stream.read(_MAX_BYTES + 1)
        data = json.loads(raw)
        if (not stat.S_ISREG(meta.st_mode) or len(raw) > _MAX_BYTES or not isinstance(data, dict)
                or set(data) - {"version", "revision", "token", "profile_ids", "updated_at"}
                or data.get("version") != 1 or not isinstance(data.get("revision"), str)
                or not _REVISION.fullmatch(data["revision"])
                or not isinstance(data.get("token", ""), str)
                or not isinstance(data.get("profile_ids", ""), str)
                or (data.get("profile_ids") and not _PROFILES.fullmatch(data["profile_ids"]))
                or not isinstance(data.get("updated_at"), str)
                or (os.name != "nt" and stat.S_IMODE(meta.st_mode) & 0o077)):
            raise ValueError
        return data, data["revision"]
    except (OSError, ValueError, TypeError, RecursionError):
        raise SparkXConfigError() from None


def _effective(settings, saved):
    token = saved.get("token") or _secret(getattr(settings, "sparkx_ai_token", ""))
    profiles = saved["profile_ids"] if "profile_ids" in saved else getattr(settings, "sparkx_profile_ids", "")
    return token, profiles


def with_console_credentials(settings):
    """Return settings whose SparkX token/profile IDs include the owner-console values.

    A missing file keeps the deployment environment. An unreadable or unsafe file
    disables SparkX (empty token) rather than silently using another credential.
    """
    path = _path(settings)
    if path is None:
        return settings
    try:
        saved, _ = _read(path)
    except SparkXConfigError:
        token, profiles = "", ""
    else:
        if not saved:
            return settings
        token, profiles = _effective(settings, saved)
    update = {"sparkx_ai_token": SecretStr(token), "sparkx_profile_ids": profiles}
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=update)
    return SimpleNamespace(**{**vars(settings), **update})


def configuration(settings):
    saved, revision = _read(_path(settings))
    token, profiles = _effective(settings, saved)
    source = ("console" if saved.get("token") else "environment" if token else None)
    count = len([part for part in profiles.split(",") if part.strip()]) if profiles else 0
    return {"revision": revision, "configured": bool(token), "token_source": source,
            "profile_ids": profiles, "profile_count": count,
            "updated_at": saved.get("updated_at"), "endpoint": "https://mcp.sparkx.cn/mcp"}


@contextmanager
def _locked(path):
    if path is None or path.is_symlink() or path.parent.is_symlink():
        raise SparkXConfigError()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise SparkXConfigError()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SparkXConfigError()
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
        except OSError:
            raise SparkXConfigError("SparkX 设置正在更新，请稍后刷新。", 409) from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def _normalize_profiles(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise SparkXConfigError("SparkX Profile ID 需为逗号分隔的数字。", 400)
    parts = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if any(not re.fullmatch(r"[1-9][0-9]{0,18}", part) for part in parts) or len(parts) > 200:
        raise SparkXConfigError("SparkX Profile ID 需为逗号分隔的数字。", 400)
    return ",".join(dict.fromkeys(parts))


def save_configuration(settings, payload):
    from jx_alert.sparkx_direct import SparkXDirectError, validate_token

    if not isinstance(payload, dict) or set(payload) != {"revision", "token", "profile_ids"}:
        raise SparkXConfigError("提交的 SparkX 设置无效。", 400)
    revision, token = payload["revision"], payload["token"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise SparkXConfigError("页面配置版本无效，请刷新。", 400)
    if not isinstance(token, str) or len(token) > 4096:
        raise SparkXConfigError("请核对 SparkX MCP Token。", 400)
    profiles = _normalize_profiles(payload["profile_ids"])
    token = token.strip()
    if token:
        try:
            validate_token(token)
        except SparkXDirectError:
            raise SparkXConfigError("SparkX MCP Token 格式无效。", 400) from None
    path = _path(settings)
    try:
        with _locked(path):
            saved, current = _read(path)
            if revision != current:
                raise SparkXConfigError("SparkX 设置已更新，请刷新后重新填写。", 409)
            update = {"version": 1, "revision": secrets.token_hex(32), "profile_ids": profiles,
                      "updated_at": datetime.now(timezone.utc).isoformat()}
            if token or saved.get("token"):
                update["token"] = token or saved["token"]
            fd, temporary = tempfile.mkstemp(prefix=".sparkx-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    if os.name != "nt":
                        os.fchmod(stream.fileno(), 0o600)
                    json.dump(update, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _read(path)[1] != current:
                    raise SparkXConfigError("SparkX 设置已更新，请刷新后重新填写。", 409)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    except OSError:
        raise SparkXConfigError("SparkX 私有配置无法保存，请联系管理员。") from None
    return configuration(settings)


def _profiles(result):
    """Parse the documented get_user_authorized_context roster; never return user/tenant IDs."""
    views = [result.structuredContent] if result.structuredContent is not None else []
    for block in result.content:
        if getattr(block, "type", None) != "text":
            continue
        text = block.text.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        if text.startswith("{"):
            views.append(json.loads(text))
    for view in views:
        if (isinstance(view, dict) and view.get("isError") is False
                and isinstance(view.get("profiles"), list)):
            rows = []
            for row in view["profiles"][:500]:
                value = row.get("profileId") if isinstance(row, dict) else None
                if type(value) is str and value.isascii() and value.isdecimal():
                    value = int(value)
                if type(value) is not int or value <= 0:
                    raise ValueError
                name, country = row.get("profileName"), row.get("countryCode")
                rows.append({"profile_id": str(value),
                             "name": name[:80] if isinstance(name, str) else "",
                             "country": country[:8] if isinstance(country, str) else ""})
            return rows
    raise ValueError


def check_connection(settings):
    """Live read-only check via get_user_authorized_context. Returns codes, never bodies."""
    import anyio

    from jx_alert.sparkx_direct import SparkXDirectError, call_sparkx, validate_token

    effective = with_console_credentials(settings)
    token = _secret(getattr(effective, "sparkx_ai_token", ""))
    checked = datetime.now(timezone.utc).isoformat()
    if not token:
        return {"status": "NOT_CONFIGURED", "checked_at": checked}
    try:
        validate_token(token)
    except SparkXDirectError:
        return {"status": "INVALID_TOKEN_FORMAT", "checked_at": checked}

    async def probe():
        return await call_sparkx("get_user_authorized_context", {}, token=token, timeout_seconds=20)

    try:
        result = anyio.run(probe)
    except SparkXDirectError as exc:
        return {"status": exc.code or "REQUEST_FAILED", "checked_at": checked}
    if result.isError:
        return {"status": "AUTH_REJECTED", "checked_at": checked}
    try:
        profiles = _profiles(result)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {"status": "RESULT_UNREADABLE", "checked_at": checked}
    raw = getattr(effective, "sparkx_profile_ids", "") or ""
    allowed = [part.strip() for part in raw.split(",") if part.strip()]
    authorized = {row["profile_id"] for row in profiles}
    for row in profiles:
        row["allowed"] = row["profile_id"] in allowed
    return {"status": "OK", "checked_at": checked, "profile_count": len(profiles),
            "profiles": profiles[:200], "allowed_count": len(allowed),
            "allowed_not_authorized": [pid for pid in allowed if pid not in authorized]}
