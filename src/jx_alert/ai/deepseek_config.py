"""Private DeepSeek configuration and its redacted owner-console projection."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from jx_alert.storage.job_lock import _try_file_lock, _unlock_file


MODELS = ("deepseek-flash", "deepseek-v4-pro")
_EMPTY_REVISION = "0" * 64
_MAX_BYTES = 64 * 1024
_REVISION = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?\+00:00")


class DeepSeekConfigError(RuntimeError):
    def __init__(self, detail="DeepSeek 私有配置无法读取，请联系管理员。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def _path(settings):
    name = getattr(settings, "deepseek_credentials_file", "")
    return Path(name) if name else None


def _read(path):
    if path is None:
        return {}, _EMPTY_REVISION
    if path.is_symlink() or path.parent.is_symlink():
        raise DeepSeekConfigError()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {}, _EMPTY_REVISION
    except OSError:
        raise DeepSeekConfigError() from None
    try:
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > _MAX_BYTES:
                raise ValueError
            raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        if "version" not in data:
            # Legacy installations predate the console. Keep the legacy format,
            # order of precedence and permission behavior until an owner saves.
            mark = f"{meta.st_dev}:{meta.st_ino}:{meta.st_mtime_ns}:{meta.st_size}"
            return data, hashlib.sha256(mark.encode()).hexdigest()
        if (
            type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data.get("revision"), str) or not _REVISION.fullmatch(data["revision"])
            or data.get("model") not in MODELS or not _valid_timeout(data.get("timeout"))
            or not isinstance(data.get("updated_at"), str)
            or not _TIMESTAMP.fullmatch(data["updated_at"])
            or not isinstance(data.get("api_key", ""), str)
            or type(data.get("thinking", True)) is not bool
            or (os.name != "nt" and stat.S_IMODE(meta.st_mode) & 0o077)
        ):
            raise ValueError
        return data, data["revision"]
    except (OSError, ValueError, TypeError, RecursionError):
        raise DeepSeekConfigError() from None


def _valid_timeout(value):
    return type(value) in {int, float} and 1 <= value <= 300 and math.isfinite(value)


def _env_key(settings):
    secret = getattr(settings, "deepseek_api_key", "")
    return secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret


def _effective(settings, saved):
    fallback_model = getattr(settings, "deepseek_model", "deepseek-flash")
    fallback_timeout = getattr(settings, "deepseek_request_timeout", 45.0)
    if saved.get("version") == 1:
        # An explicitly replaced console key wins, but saving model/timeout
        # alone must not copy a deployment-managed environment secret to disk.
        return saved.get("api_key") or _env_key(settings), saved["model"], saved["timeout"]
    return _env_key(settings) or saved.get("api_key", ""), fallback_model, fallback_timeout


def client_settings(settings):
    """Return the effective key, model and timeout for a runtime request."""
    saved, _ = _read(_path(settings))
    return _effective(settings, saved)


def options(settings):
    """Owner options beyond key/model/timeout. Thinking mode is on unless the owner turned it off."""
    saved, _ = _read(_path(settings))
    return {"thinking": saved.get("thinking", True) if saved.get("version") == 1 else True}


def cache_identity(settings):
    """Fence analysis caches with the opaque owner-config revision, never the key."""
    try:
        saved, revision = _read(_path(settings))
    except DeepSeekConfigError:
        return "deepseek:configuration-unavailable"
    _, model, _ = _effective(settings, saved)
    # Preserve key compatibility for the existing environment-only defaults.
    return model if revision == _EMPTY_REVISION else f"{model}:{revision}"


def _sources(settings, saved):
    """Where the effective key and model come from; never the values themselves."""
    console = saved.get("version") == 1
    if console and saved.get("api_key"):
        key = "console"
    elif _env_key(settings):
        key = "environment"
    elif saved.get("api_key"):
        key = "private_file"
    else:
        key = None
    return key, "console" if console else "environment"


def _projection(settings, saved, revision):
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError

    key, model, timeout = _effective(settings, saved)
    status = "NOT_CONFIGURED"
    if key:
        try:
            DeepSeekClient(key, model=model, timeout=timeout)
        except DeepSeekError:
            status = "INVALID_CONFIGURATION"
        else:
            status = "CONFIGURED"
    key_source, model_source = _sources(settings, saved)
    return {
        "revision": revision,
        "status": status,
        "configured": status == "CONFIGURED",
        "model": model if model in MODELS else None,
        "timeout": timeout if _valid_timeout(timeout) else None,
        "thinking": saved.get("thinking", True) if saved.get("version") == 1 else True,
        "models": list(MODELS),
        "updated_at": saved.get("updated_at") if saved.get("version") == 1 else None,
        "key_source": key_source if key else None,
        "model_source": model_source,
    }


_API = "https://api.deepseek.com"
_TEST_ERRORS = {401: "INVALID_KEY", 402: "INSUFFICIENT_BALANCE", 403: "FORBIDDEN", 429: "RATE_LIMITED"}


def check_connection(settings, *, transport=None):
    """No-cost check of the effective settings: List Models and Get Balance only.

    https://api-docs.deepseek.com/api/list-models
    https://api-docs.deepseek.com/api/get-user-balance
    No chat completion is sent, so no tokens are billed. Provider text is never returned.
    """
    import httpx

    from jx_alert.ai.deepseek import DeepSeekError, _validated_key

    checked = datetime.now(timezone.utc).isoformat()
    try:
        key, model, timeout = client_settings(settings)
    except DeepSeekConfigError:
        return {"status": "CONFIGURATION_UNAVAILABLE", "checked_at": checked}
    if not key:
        return {"status": "NOT_CONFIGURED", "checked_at": checked}
    try:
        key = _validated_key(key)
    except DeepSeekError:
        return {"status": "INVALID_CONFIGURATION", "checked_at": checked}
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=min(float(timeout), 15.0), follow_redirects=False, trust_env=False,
                          transport=transport) as client:
            listed = client.get(_API + "/models", headers=headers)
            if listed.status_code != 200:
                status = _TEST_ERRORS.get(listed.status_code, "SERVICE_UNAVAILABLE")
                return {"status": status, "checked_at": checked, "model": model}
            if len(listed.content) > 256 * 1024:
                raise ValueError
            data = listed.json().get("data")
            if not isinstance(data, list):
                raise ValueError
            ids = sorted({row["id"] for row in data if isinstance(row, dict) and isinstance(row.get("id"), str)})
            balance = None
            response = client.get(_API + "/user/balance", headers=headers)
            if response.status_code == 200 and len(response.content) <= 64 * 1024:
                value = response.json().get("is_available")
                balance = value if type(value) is bool else None
    except httpx.TimeoutException:
        return {"status": "TIMEOUT", "checked_at": checked, "model": model}
    except httpx.HTTPError:
        return {"status": "UNREACHABLE", "checked_at": checked, "model": model}
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"status": "RESPONSE_INVALID", "checked_at": checked, "model": model}
    available = model in ids
    status = ("MODEL_UNAVAILABLE" if not available else
              "INSUFFICIENT_BALANCE" if balance is False else "OK")
    return {"status": status, "checked_at": checked, "model": model, "model_available": available,
            "balance_available": balance, "listed_models": [name for name in ids if name in MODELS]}


def configuration(settings):
    saved, revision = _read(_path(settings))
    return _projection(settings, saved, revision)


@contextmanager
def _locked(path):
    if path is None or path.is_symlink() or path.parent.is_symlink():
        raise DeepSeekConfigError()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise DeepSeekConfigError()
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DeepSeekConfigError()
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
        except OSError:
            raise DeepSeekConfigError("DeepSeek 设置正在更新，请稍后刷新。", 409) from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def _replace(path, value, expected_revision):
    fd, temporary = tempfile.mkstemp(prefix=".deepseek-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(path)[1] != expected_revision:
            raise DeepSeekConfigError("DeepSeek 设置已更新，请刷新后重新填写。", 409)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_configuration(settings, payload):
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError

    if not isinstance(payload, dict) or not ({"revision", "api_key", "model", "timeout"} <= set(payload)
                                             <= {"revision", "api_key", "model", "timeout", "thinking"}):
        raise DeepSeekConfigError("提交的 DeepSeek 设置无效。", 400)
    revision, key, model, timeout = (payload[name] for name in ("revision", "api_key", "model", "timeout"))
    thinking = payload.get("thinking", True)
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise DeepSeekConfigError("页面配置版本无效，请刷新。", 400)
    if (not isinstance(key, str) or len(key) > 512 or model not in MODELS or not _valid_timeout(timeout)
            or type(thinking) is not bool):
        raise DeepSeekConfigError("请核对 DeepSeek 密钥、模型、超时和深度思考设置。", 400)
    path = _path(settings)
    try:
        with _locked(path):
            saved, current_revision = _read(path)
            if revision != current_revision:
                raise DeepSeekConfigError("DeepSeek 设置已更新，请刷新后重新填写。", 409)
            previous_key = saved.get("api_key", "") if saved.get("version") == 1 or not _env_key(settings) else ""
            effective_key = key if key else previous_key or _env_key(settings)
            try:
                DeepSeekClient(effective_key, model=model, timeout=timeout)
            except DeepSeekError as exc:
                raise DeepSeekConfigError(str(exc), 400) from None
            update = {
                "version": 1, "revision": secrets.token_hex(32), "model": model,
                "timeout": timeout, "thinking": thinking, "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if key or previous_key:
                update["api_key"] = effective_key.strip()
            _replace(path, update, current_revision)
            return _projection(settings, update, update["revision"])
    except OSError:
        raise DeepSeekConfigError("DeepSeek 私有配置无法保存，请联系管理员。") from None
