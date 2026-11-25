"""The owner's 「广告写入」 switch: SparkX ad changes through the Feishu bot (owner decision, Nov 2026).

Off by default, and off whenever the file cannot be read. When the owner switches it on in the
console, the 负责人 (总负责人 and 广告负责人) may ask the Feishu bot to change ads: DeepSeek then
gets the write tools and every official write Skill, and a change runs only in the turn where
the requester answered 「确认执行」 to the plan of the previous turn (see ai.agent and
bot_service). Scheduled lanes, console previews and test sends never write.
"""
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets

from jx_alert.storage import private_file

_EMPTY_REVISION = "0" * 64
_REVISION = re.compile(r"[0-9a-f]{64}")
CONFIRM_WORD = "确认执行"


class WriteModeError(RuntimeError):
    def __init__(self, detail="写入开关无法读取，请联系管理员。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def _path(settings):
    return Path(getattr(settings, "sparkx_write_file", "/app/data/sparkx-write.json"))


def load(settings):
    """{"revision", "enabled", "updated_at", "enabled_at"}; raises WriteModeError when unreadable."""
    try:
        data = private_file.read_json(_path(settings), max_bytes=16 * 1024)
    except ValueError:
        raise WriteModeError() from None
    if data is None:
        return {"revision": _EMPTY_REVISION, "enabled": False, "updated_at": None, "enabled_at": None}
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("revision"), str)
            or not _REVISION.fullmatch(data["revision"]) or type(data.get("enabled")) is not bool):
        raise WriteModeError()
    return {"revision": data["revision"], "enabled": data["enabled"], "updated_at": data.get("updated_at"),
            "enabled_at": data.get("enabled_at")}


def enabled(settings):
    """True only when the owner switched writing on; any doubt means off."""
    try:
        return load(settings)["enabled"]
    except WriteModeError:
        return False


def save(settings, enabled_value, revision):
    if type(enabled_value) is not bool or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise WriteModeError("提交的写入设置无效，请刷新。", 400)
    path = _path(settings)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with private_file.locked(path):
            current = load(settings)
            if current["revision"] != revision:
                raise WriteModeError("写入设置已更新，请刷新后重试。", 409)
            candidate = {"version": 1, "revision": secrets.token_hex(32), "enabled": enabled_value, "updated_at": now,
                         "enabled_at": (current["enabled_at"] if current["enabled"] else now) if enabled_value else None}
            private_file.replace_json(path, candidate)
    except private_file.Busy:
        raise WriteModeError("写入设置正在保存，请稍后刷新。", 409) from None
    except (OSError, ValueError):
        raise WriteModeError("写入设置无法保存，请检查 data 目录权限。", 503) from None
    return load(settings)


def confirms(question):
    """Whether a message is exactly 「确认执行」 (spaces and punctuation aside), the explicit yes to a plan.

    Anything longer ("确认执行，但预算改成 50") is a new request: it gets a new plan, not an execution.
    """
    return isinstance(question, str) and re.sub(r"[\W_]+", "", question) == CONFIRM_WORD


# SparkX's two-phase writes return a confirmToken; its Skill says never to log it.
_TOKEN_TEXT = re.compile(r'("confirm_?token"\s*:\s*)"[^"]*"', re.IGNORECASE)
_SECRET_KEYS = {"confirmtoken", "confirm_token"}


def redact(value):
    """A copy of a write request with every confirmToken replaced, for the audit record."""
    if isinstance(value, dict):
        return {key: "***" if isinstance(key, str) and key.lower() in _SECRET_KEYS else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_text(text):
    """A SparkX result text with every confirmToken value replaced."""
    return _TOKEN_TEXT.sub(r'\1"***"', text) if isinstance(text, str) else text
