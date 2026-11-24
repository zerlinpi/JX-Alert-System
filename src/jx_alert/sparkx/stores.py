"""SparkX store directory: one entry per SparkX profile (store × site).

The owner decides per profile whether it is monitored and who operates it
(Feishu Open IDs verified against the report group). Profiles authorized by
the token but never edited are monitored by default and have no operator.
The live roster (names, sites) comes from get_user_authorized_context and is
kept as a SyncJob snapshot, so the console works while SparkX is unreachable.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import secrets
import unicodedata

from jx_alert.storage import private_file

ROSTER_JOB = "sparkx_roster"
UNMAPPED = "未映射运营"
COUNTRY_LABELS = {"US": "美国", "CA": "加拿大", "MX": "墨西哥", "BR": "巴西", "UK": "英国", "GB": "英国",
                  "DE": "德国", "FR": "法国", "IT": "意大利", "ES": "西班牙", "JP": "日本", "AU": "澳大利亚",
                  "NL": "荷兰", "SE": "瑞典", "PL": "波兰", "BE": "比利时", "SG": "新加坡", "AE": "阿联酋",
                  "SA": "沙特", "IN": "印度", "TR": "土耳其", "EG": "埃及", "ZA": "南非"}
_EMPTY_REVISION = "0" * 64
_REVISION = re.compile(r"[0-9a-f]{64}")
_PID = re.compile(r"[1-9][0-9]{0,19}")
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,197}")


class StoreDirectoryError(RuntimeError):
    def __init__(self, detail="店铺目录无法读取，请联系管理员。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def clean_name(value, limit=40):
    text = "".join(c for c in str(value or "") if unicodedata.category(c) not in {"Cc", "Cf", "Cs"})
    return text.strip()[:limit]


def _path(settings):
    return Path(getattr(settings, "sparkx_stores_file", "/app/data/sparkx-stores.json"))


def _valid_entry(entry):
    operators = entry.get("operators") if isinstance(entry, dict) else None
    return (isinstance(entry, dict) and set(entry) <= {"monitored", "operators", "updated_at"}
            and type(entry.get("monitored")) is bool and isinstance(operators, list) and len(operators) <= 10
            and all(isinstance(row, dict) and set(row) == {"open_id", "name"}
                    and isinstance(row["open_id"], str) and _OPEN_ID.fullmatch(row["open_id"])
                    and isinstance(row["name"], str) and len(row["name"]) <= 40 for row in operators)
            and len({row["open_id"] for row in operators}) == len(operators))


def _valid(data):
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("revision"), str)
            or not _REVISION.fullmatch(data["revision"]) or not isinstance(data.get("profiles"), dict)
            or len(data["profiles"]) > 2000
            or any(not isinstance(key, str) or not _PID.fullmatch(key) or not _valid_entry(value)
                   for key, value in data["profiles"].items())):
        raise ValueError
    return data


def load(settings):
    try:
        data = private_file.read_json(_path(settings))
        if data is None:
            return {"version": 1, "revision": _EMPTY_REVISION, "updated_at": None, "profiles": {}}
        return _valid(data)
    except (OSError, ValueError, TypeError):
        raise StoreDirectoryError() from None


def monitored(directory, pid):
    entry = directory["profiles"].get(pid)
    return True if entry is None else entry["monitored"]


def operators(directory, pid):
    entry = directory["profiles"].get(pid)
    return [] if entry is None else list(entry["operators"])


def operator_label(directory, pid):
    names = [row["name"] or row["open_id"] for row in operators(directory, pid)]
    return "、".join(names) if names else UNMAPPED


def save(settings, changes, revision):
    """Apply {pid: {"monitored": bool, "operators": [{open_id, name}]}} with compare-and-swap."""
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise StoreDirectoryError("页面版本无效，请刷新。", 400)
    if not isinstance(changes, dict) or not 1 <= len(changes) <= 2000:
        raise StoreDirectoryError("提交的店铺设置无效。", 400)
    stamp = datetime.now(timezone.utc).isoformat()
    path = _path(settings)
    try:
        with private_file.locked(path):
            current = load(settings)
            if current["revision"] != revision:
                raise StoreDirectoryError("店铺目录已更新，请刷新后重新保存。", 409)
            profiles = dict(current["profiles"])
            for pid, value in changes.items():
                entry = {"monitored": value.get("monitored") if isinstance(value, dict) else None,
                         "operators": [{"open_id": row.get("open_id"), "name": clean_name(row.get("name"))}
                                       for row in (value.get("operators") or [])
                                       if isinstance(row, dict)] if isinstance(value, dict) else None,
                         "updated_at": stamp}
                if not isinstance(pid, str) or not _PID.fullmatch(pid) or not _valid_entry(entry):
                    raise StoreDirectoryError("店铺设置无效：每家店最多 10 位运营，且须为飞书成员。", 400)
                profiles[pid] = entry
            candidate = {"version": 1, "revision": secrets.token_hex(32), "updated_at": stamp,
                         "profiles": dict(sorted(profiles.items(), key=lambda item: int(item[0])))}
            private_file.replace_json(path, candidate)
    except private_file.Busy:
        raise StoreDirectoryError("店铺目录正在更新，请稍后刷新。", 409) from None
    except (OSError, ValueError):
        raise StoreDirectoryError("店铺目录无法保存，请检查运行目录权限。") from None
    return candidate


# ---------------------------------------------------------------- roster snapshots

def latest_roster(db):
    from sqlalchemy import select

    from jx_alert.storage.models import SyncJob

    job = db.scalar(select(SyncJob).where(SyncJob.job_type == ROSTER_JOB, SyncJob.status == "SUCCEEDED")
                    .order_by(SyncJob.id.desc()).limit(1))
    if job is None:
        return {}, None
    profiles = (job.metadata_json or {}).get("profiles")
    return (profiles if isinstance(profiles, dict) else {}), job.started_at


def record_roster(db, profiles, now):
    """Keep one snapshot per change (or per day), not one per hourly run."""
    from jx_alert.storage.models import SyncJob

    previous, at = latest_roster(db)
    instant = now.astimezone(timezone.utc).replace(tzinfo=None)
    if previous == profiles and at is not None and instant - at < timedelta(hours=24):
        return False
    db.add(SyncJob(job_type=ROSTER_JOB, status="SUCCEEDED", started_at=instant, finished_at=instant,
                   store_count=len(profiles), metadata_json={"profiles": profiles}))
    return True


# ---------------------------------------------------------------- display names

def short_name(name):
    """Display name in the agent style: drop '-主账号' and a trailing site code."""
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    text = re.sub(r"[-_ ]?主账号", "", text)
    text = re.sub(r"(?:[-_ ](?:US|CA|MX|BR|UK|DE|FR|IT|ES|JP|AU))+$", "", text, flags=re.IGNORECASE)
    return text.strip(" -_") or str(name or "")


def _look(value):
    """What a reader tells names apart by: not case, spacing or punctuation."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value)).casefold()


def display_names(profiles):
    """Short names (non-US keep their site), falling back to full names on collision.

    A Vendor account whose name reads like another store's (for example the same company
    name in capitals) is marked （Vendor）, so a report never seems to list one store twice.
    """
    short = {}
    for pid, profile in profiles.items():
        value = short_name(profile["name"])
        if profile.get("country") not in (None, "US", "未知"):
            value += f"（{COUNTRY_LABELS.get(profile['country'], profile['country'])}）"
        short[pid] = value
    counts = defaultdict(int)
    for value in short.values():
        counts[value] += 1
    names = {pid: (value if counts[value] == 1 else profiles[pid]["name"]) for pid, value in short.items()}
    alike = defaultdict(list)
    for pid, value in names.items():
        alike[_look(value)].append(pid)
    for pids in alike.values():
        if len(pids) > 1:
            for pid in pids:
                if profiles[pid].get("store_type") == "vendor":
                    names[pid] += "（Vendor）"
    return names

