"""Owner switches, recipients, thresholds and DeepSeek task text of the three lanes (0600 file).

Every lane is on by default (owner decision, Nov 2026): it always reaches the
report group, also while no store has an operator yet; once operators are
mapped, the group message @-mentions the operators of the affected stores
(the stores the daily report flags, the review's anomalies, the hourly alerts).
The lead (总负责人 for the daily report, 广告负责人 for the review and the
hourly monitor) also gets a private copy; private operator copies are optional.
``prompt`` is the owner's task text for DeepSeek; empty means the built-in
default (reports.tasks.DEFAULT_PROMPTS), so improved defaults reach every install.
"""
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets

from jx_alert.storage import private_file

LANES = ("daily", "review", "hourly")
HOURLY_DEFAULTS = {"high_spend": 30, "low_spend": 10, "min_clicks": 15, "acos_high": 60, "cvr_low": 5,
                   "acos_good": 20, "min_orders_good": 2}
_EMPTY_REVISION = "0" * 64
_REVISION = re.compile(r"[0-9a-f]{64}")
DEFAULTS = {
    "daily": {"enabled": True, "to_group": True, "mention_operators": True, "to_lead": True, "prompt": ""},
    "review": {"enabled": True, "to_group": True, "mention_operators": True, "to_lead": True,
               "to_operators": False, "weekly": False, "prompt": ""},
    "hourly": {"enabled": True, "to_group": True, "mention_operators": True, "to_lead": True,
               "to_operators": False, "start_hour": 9, "end_hour": 24, **HOURLY_DEFAULTS, "prompt": ""},
}
# Fields of earlier releases that no longer have an effect (the rule-based text switch).
_RETIRED = {"ai"}
_NUMBERS = {"high_spend": (1, 100_000), "low_spend": (0, 100_000), "min_clicks": (1, 10_000),
            "acos_high": (1, 1000), "cvr_low": (0, 100), "acos_good": (1, 100), "min_orders_good": (1, 1000)}
MAX_PROMPT = 4000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SettingsError(RuntimeError):
    def __init__(self, detail="通知设置无法读取，请联系管理员。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def _path(settings):
    return Path(getattr(settings, "notification_settings_file", "/app/data/jx-notifications.json"))


def _number(value, low, high):
    return type(value) in {int, float} and low <= value <= high


def validate(lanes):
    if not isinstance(lanes, dict) or set(lanes) != set(LANES):
        raise ValueError
    for lane, defaults in DEFAULTS.items():
        value = lanes[lane]
        if not isinstance(value, dict) or set(value) != set(defaults):
            raise ValueError
        for key, default in defaults.items():
            if type(default) is bool and type(value[key]) is not bool:
                raise ValueError
        prompt = value["prompt"]
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT or _CONTROL.search(prompt):
            raise ValueError
    hourly = lanes["hourly"]
    if (type(hourly["start_hour"]) is not int or type(hourly["end_hour"]) is not int
            or not 0 <= hourly["start_hour"] < hourly["end_hour"] <= 24):
        raise ValueError
    for key, (low, high) in _NUMBERS.items():
        if not _number(hourly[key], low, high):
            raise ValueError
    if hourly["low_spend"] >= hourly["high_spend"]:
        raise ValueError
    return lanes


def _current(saved, lane):
    values = {key: value for key, value in (saved or {}).items() if key not in _RETIRED}
    return {**DEFAULTS[lane], **values}


def load(settings):
    try:
        data = private_file.read_json(_path(settings), max_bytes=128 * 1024)
        if data is None:
            return {"version": 1, "revision": _EMPTY_REVISION, "updated_at": None,
                    "lanes": {lane: dict(values) for lane, values in DEFAULTS.items()}}
        if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("revision"), str)
                or not _REVISION.fullmatch(data["revision"])):
            raise ValueError
        # Fields added by later releases start from their default value.
        lanes = {lane: _current((data.get("lanes") or {}).get(lane, {}), lane) for lane in LANES}
        validate(lanes)
        return {**data, "lanes": lanes}
    except (OSError, ValueError, TypeError, AttributeError):
        raise SettingsError() from None


def save(settings, payload):
    if not isinstance(payload, dict) or set(payload) != {"revision", "lanes"}:
        raise SettingsError("提交的通知设置无效。", 400)
    if not isinstance(payload["revision"], str) or not _REVISION.fullmatch(payload["revision"]):
        raise SettingsError("页面配置版本无效，请刷新。", 400)
    try:
        if any(set(payload["lanes"][lane]) - set(DEFAULTS[lane]) for lane in LANES):
            raise ValueError
        lanes = validate({lane: {**DEFAULTS[lane], **payload["lanes"][lane]} for lane in LANES})
        for lane in LANES:
            lanes[lane]["prompt"] = lanes[lane]["prompt"].strip()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SettingsError("请核对开关、发送对象、阈值与任务说明（低花费须小于高花费，时段 0–24 点，"
                            f"任务说明不超过 {MAX_PROMPT} 字）。", 400) from None
    path = _path(settings)
    candidate = {"version": 1, "revision": secrets.token_hex(32),
                 "updated_at": datetime.now(timezone.utc).isoformat(), "lanes": lanes}
    try:
        with private_file.locked(path):
            if load(settings)["revision"] != payload["revision"]:
                raise SettingsError("通知设置已更新，请刷新后重新保存。", 409)
            private_file.replace_json(path, candidate)
    except private_file.Busy:
        raise SettingsError("通知设置正在更新，请稍后刷新。", 409) from None
    except (OSError, ValueError):
        raise SettingsError("通知设置无法保存，请检查运行目录权限。") from None
    return candidate
