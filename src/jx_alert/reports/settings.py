"""Owner switches, recipients and thresholds of the three notification lanes (0600 file).

Every lane is off until the owner enables it in the console. Recipients are
chosen per lane: the report group, the lead (总负责人 for the daily report,
广告负责人 for the review and the hourly monitor) and the stores' operators.
"""
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets

from jx_alert.sparkx.analysis import HOURLY_DEFAULTS
from jx_alert.storage import private_file

LANES = ("daily", "review", "hourly")
_EMPTY_REVISION = "0" * 64
_REVISION = re.compile(r"[0-9a-f]{64}")
DEFAULTS = {
    "daily": {"enabled": False, "to_group": False, "to_lead": True, "ai": True},
    "review": {"enabled": False, "to_group": False, "to_lead": True, "to_operators": True, "ai": True,
               "weekly": False},
    "hourly": {"enabled": False, "to_group": False, "to_lead": True, "to_operators": True,
               "start_hour": 9, "end_hour": 24, **HOURLY_DEFAULTS},
}
_NUMBERS = {"high_spend": (1, 100_000), "low_spend": (0, 100_000), "min_clicks": (1, 10_000),
            "acos_high": (1, 1000), "cvr_low": (0, 100), "acos_good": (1, 100), "min_orders_good": (1, 1000)}


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


def load(settings):
    try:
        data = private_file.read_json(_path(settings), max_bytes=64 * 1024)
        if data is None:
            return {"version": 1, "revision": _EMPTY_REVISION, "updated_at": None,
                    "lanes": {lane: dict(values) for lane, values in DEFAULTS.items()}}
        if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("revision"), str)
                or not _REVISION.fullmatch(data["revision"])):
            raise ValueError
        # Fields added by later releases start from their default value.
        lanes = {lane: {**DEFAULTS[lane], **(data.get("lanes") or {}).get(lane, {})} for lane in LANES}
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
        lanes = validate({lane: {**DEFAULTS[lane], **payload["lanes"][lane]} for lane in LANES})
        if any(set(payload["lanes"][lane]) - set(DEFAULTS[lane]) for lane in LANES):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SettingsError("请核对开关、发送对象与阈值（低花费须小于高花费，时段 0–24 点）。", 400) from None
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
