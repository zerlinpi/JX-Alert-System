"""Owner-console SparkX MCP token, with the deployment environment as fallback.

The token is write-only: it is stored in a private 0600 file and never returned.
An unreadable or unsafe file disables SparkX instead of silently falling back to
another credential. Only get_user_authorized_context is used to test it.
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

from jx_alert.storage.job_lock import _try_file_lock, _unlock_file

ENDPOINT = "https://mcp.sparkx.cn/mcp"
_EMPTY_REVISION = "0" * 64
_MAX_BYTES = 16 * 1024
_REVISION = re.compile(r"[0-9a-f]{64}")
# profile_ids was written by the previous release; it is read and ignored.
_FIELDS = {"version", "revision", "token", "profile_ids", "updated_at"}


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
                or set(data) - _FIELDS or data.get("version") != 1
                or not isinstance(data.get("revision"), str) or not _REVISION.fullmatch(data["revision"])
                or not isinstance(data.get("token", ""), str)
                or not isinstance(data.get("updated_at"), str)
                or (os.name != "nt" and stat.S_IMODE(meta.st_mode) & 0o077)):
            raise ValueError
        return data, data["revision"]
    except (OSError, ValueError, TypeError, RecursionError):
        raise SparkXConfigError() from None


def effective_token(settings):
    """The token to use now, or "" (missing file keeps the environment; a bad file disables)."""
    try:
        saved, _ = _read(_path(settings))
    except SparkXConfigError:
        return ""
    return saved.get("token") or _secret(getattr(settings, "sparkx_ai_token", ""))


def configuration(settings):
    saved, revision = _read(_path(settings))
    token = saved.get("token") or _secret(getattr(settings, "sparkx_ai_token", ""))
    source = "console" if saved.get("token") else "environment" if token else None
    return {"revision": revision, "configured": bool(token), "token_source": source,
            "updated_at": saved.get("updated_at"), "endpoint": ENDPOINT}


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


def save_configuration(settings, payload):
    """Replace (or clear with clear=true) the console token with compare-and-swap."""
    from jx_alert.sparkx.client import validate_token

    if not isinstance(payload, dict) or set(payload) - {"revision", "token", "clear"} or "revision" not in payload:
        raise SparkXConfigError("提交的 SparkX 设置无效。", 400)
    revision, token, clear = payload["revision"], payload.get("token", ""), payload.get("clear", False)
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise SparkXConfigError("页面配置版本无效，请刷新。", 400)
    if not isinstance(token, str) or len(token) > 4096 or type(clear) is not bool:
        raise SparkXConfigError("请核对 SparkX MCP Token。", 400)
    token = token.strip()
    if token and not validate_token(token):
        raise SparkXConfigError("SparkX MCP Token 格式无效。", 400)
    if not token and not clear:
        raise SparkXConfigError("请填写新的 SparkX MCP Token。", 400)
    path = _path(settings)
    try:
        with _locked(path):
            _, current = _read(path)
            if revision != current:
                raise SparkXConfigError("SparkX 设置已更新，请刷新后重新填写。", 409)
            update = {"version": 1, "revision": secrets.token_hex(32),
                      "updated_at": datetime.now(timezone.utc).isoformat()}
            if token:
                update["token"] = token
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


def check_connection(settings, *, timeout=20.0):
    """Live read-only check via get_user_authorized_context. Returns codes, never bodies."""
    from jx_alert.sparkx.client import SparkXError, authorized_profiles, open_session, run, validate_token

    token = effective_token(settings)
    checked = datetime.now(timezone.utc).isoformat()
    if not token:
        return {"status": "NOT_CONFIGURED", "checked_at": checked}
    if not validate_token(token):
        return {"status": "INVALID_TOKEN_FORMAT", "checked_at": checked}

    async def probe():
        async with open_session(token, timeout) as session:
            return await authorized_profiles(session)

    try:
        profiles = run(probe, total_timeout=timeout + 10)
    except SparkXError as exc:
        return {"status": exc.code.removeprefix("SPARKX_"), "message": exc.message, "checked_at": checked}
    rows = [{"profile_id": key, **value} for key, value in profiles.items()]
    rows.sort(key=lambda row: (row["country"] != "US", row["country"], row["name"]))
    return {"status": "OK", "checked_at": checked, "profile_count": len(rows), "profiles": rows[:500]}
