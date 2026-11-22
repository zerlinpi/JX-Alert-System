"""Owner installs a versioned JX review policy, never code or remote content."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from jx_alert.ai.ad_review_skills import DEFAULT_SKILL, parse_ad_review_skills, read_ad_review_skills_file
from jx_alert.storage.job_lock import _try_file_lock, _unlock_file


# Updating any preset behavior requires a new revision of the embedded catalog.
_CATALOG_VERSION = 1
_PRESETS = {
    "balanced": ("均衡", "兼顾流量、花费和成熟转化表现。"),
    "efficiency": ("效率优先", "优先已成熟的 CPA、ROAS 和转化效率信号。"),
    "reach": ("曝光优先", "优先看曝光、点击和流量表现。"),
    "discovery": ("探索优先", "考虑新广告探索阶段，仍遵守样本与归因限制。"),
}


class ReviewSkillError(RuntimeError):
    def __init__(self, detail, status_code):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _path(settings):
    value = getattr(settings, "ad_review_skills_file", None)
    return Path(value) if value else None


def _load(path):
    try:
        raw = read_ad_review_skills_file(path)
    except FileNotFoundError:
        return None, {"version": 1}
    return raw, parse_ad_review_skills(raw)[0]


def _revision(raw):
    state = b"missing" if raw is None else b"configured\0" + raw
    return hashlib.sha256(f"catalog:{_CATALOG_VERSION}\0".encode() + state).hexdigest()


def _public_review_skills(path, status, raw=None, document=None):
    document = document or {}
    return {
        "catalog_version": _CATALOG_VERSION,
        "status": status,
        "can_install": path is not None and status in {"default", "configured"},
        "revision": _revision(raw) if status in {"default", "configured"} else None,
        "active_preset_id": document.get("defaults", {}).get("objective", DEFAULT_SKILL["objective"])
            if status in {"default", "configured"} else None,
        "presets": [{"id": key, "label": label, "description": description}
                    for key, (label, description) in _PRESETS.items()],
        "preserved_overrides": {
            "ad_types": len(document.get("ad_types", {})),
            "campaigns": len(document.get("campaigns", {})),
        },
    }


def get_review_skills(settings):
    path = _path(settings)
    if path is None:
        return _public_review_skills(path, "disabled")
    try:
        raw, document = _load(path)
    except (OSError, ValueError, RecursionError):
        return _public_review_skills(path, "invalid")
    return _public_review_skills(path, "default" if raw is None else "configured", raw, document)


@contextmanager
def _write_lock(path):
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise OSError("unsafe lock file")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("unsafe lock file")
        try:
            _try_file_lock(fd)
        except OSError:
            raise ReviewSkillError("分析技能正在更新，请刷新重试。", 409) from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def _replace(path, raw, previous):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".jx-review-skill-", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current, _ = _load(path)
        if current != previous:
            raise ReviewSkillError("分析技能已更改，刷新后重试。", 409)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_review_skill(settings, payload):
    if not isinstance(payload, dict) or set(payload) != {"revision", "preset_id"}:
        raise ReviewSkillError("提交内容无效。", 400)
    revision, preset = payload["revision"], payload["preset_id"]
    if not isinstance(revision, str) or len(revision) != 64 or not isinstance(preset, str) or preset not in _PRESETS:
        raise ReviewSkillError("未知分析技能，请刷新后重试。", 400)
    path = _path(settings)
    if path is None:
        raise ReviewSkillError("后台未配置分析技能路径。", 409)
    try:
        with _write_lock(path):
            try:
                raw, document = _load(path)
            except (OSError, ValueError, RecursionError):
                raise ReviewSkillError("现有分析技能配置无效，未覆盖，请先修复服务端文件。", 409) from None
            if _revision(raw) != revision:
                raise ReviewSkillError("分析技能已更改，刷新后重试。", 409)
            defaults = document.get("defaults", {})
            if defaults.get("objective", DEFAULT_SKILL["objective"]) == preset:
                return _public_review_skills(path, "default" if raw is None else "configured", raw, document)
            updated = {**document, "defaults": {**defaults, "objective": preset}}
            serialized = (json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            # Keep the worker's fail-closed size and schema limits intact.
            if len(serialized) > 64 * 1024:
                serialized = json.dumps(updated, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(serialized) > 64 * 1024:
                raise ReviewSkillError("分析技能配置已达大小上限，未覆盖。", 409)
            parse_ad_review_skills(serialized)
            _replace(path, serialized, raw)
            return _public_review_skills(path, "configured", serialized, updated)
    except (OSError, ValueError, RecursionError):
        raise ReviewSkillError("无法安全保存分析技能配置。", 409) from None
