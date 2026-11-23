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


def display_names(profiles):
    """Short names (non-US keep their site), falling back to full names on collision."""
    short = {}
    for pid, profile in profiles.items():
        value = short_name(profile["name"])
        if profile.get("country") not in (None, "US", "未知"):
            value += f"（{COUNTRY_LABELS.get(profile['country'], profile['country'])}）"
        short[pid] = value
    counts = defaultdict(int)
    for value in short.values():
        counts[value] += 1
    return {pid: (value if counts[value] == 1 else profiles[pid]["name"]) for pid, value in short.items()}


# ---------------------------------------------------------------- legacy operator suggestions

_JX_COUNTRIES = {"美国": "US", "加拿大": "CA", "墨西哥": "MX", "巴西": "BR", "英国": "UK", "德国": "DE",
                 "法国": "FR", "意大利": "IT", "西班牙": "ES", "日本": "JP", "澳大利亚": "AU"}
_MARKETS = {"ATVPDKIKX0DER": "US", "A2EUQ1WTGCTBG2": "CA", "A1AM78C64UM0Y8": "MX", "A2Q3Y263D00KWC": "BR",
            "A1F83G8C2ARO7P": "UK", "A1PA6795UKMFR9": "DE", "A13V1IB3VIYZZH": "FR", "APJ6JRA9NG5V4": "IT",
            "A1RKKUPIHCS9HS": "ES", "A1VC38T7YXB528": "JP", "A39IBJ37TRP1C6": "AU"}


def _match_key(name):
    text = unicodedata.normalize("NFKC", str(name or "")).casefold()
    text = re.sub(r"主账号|小号|渠道|amz|seller|vendor|llc|inc|ltd|\b(?:us|ca|mx|br|uk|de|fr|it|es|jp)\b", "", text)
    return re.sub(r"[\W_]+", "", text)


def _common(a, b):
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def suggest_matches(profiles, stores):
    """Name-based profile→legacy store matches in the same site; the owner confirms."""
    result = {}
    for pid, profile in profiles.items():
        key = _match_key(profile.get("name"))
        scored = []
        for store in stores:
            if store["country"] != profile.get("country"):
                continue
            score = _common(key, _match_key(store["name"]))
            cjk = any("\u4e00" <= char <= "\u9fff" for char in key)
            if score >= (2 if cjk else 3):
                scored.append((score, store["sid"]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            result[pid] = scored[0][1]
    return result


def legacy_stores(db):
    """Store names from the retired warehouse table, read-only; [] when absent."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        rows = db.execute(text("SELECT sid, name, country, marketplace_id FROM stores")).all()
    except SQLAlchemyError:
        db.rollback()
        return []
    stores = []
    for sid, name, country, market in rows:
        code = _MARKETS.get(market or "") or _JX_COUNTRIES.get(country or "") or (
            country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else None)
        stores.append({"sid": str(sid), "name": str(name or sid), "country": "UK" if code == "GB" else code})
    return stores


def legacy_suggestions(db, routing, roster):
    """{pid: {"store": legacy name, "operators": [{open_id, name}]}} from the old sid mapping."""
    stores = legacy_stores(db)
    names = {row["sid"]: row["name"] for row in stores}
    mapping = routing.get("stores") if isinstance(routing, dict) else None
    mapping = mapping if isinstance(mapping, dict) else {}
    result = {}
    for pid, sid in suggest_matches(roster, stores).items():
        entry = mapping.get(sid)
        entry = {"open_ids": entry} if isinstance(entry, list) else entry if isinstance(entry, dict) else {}
        labels = entry.get("owner_display_names") if isinstance(entry.get("owner_display_names"), dict) else {}
        people = [{"open_id": uid, "name": clean_name(labels.get(uid)) or uid}
                  for uid in dict.fromkeys(entry.get("open_ids") or [])
                  if isinstance(uid, str) and _OPEN_ID.fullmatch(uid)]
        if people:
            result[pid] = {"store": names.get(sid, sid), "operators": people[:10]}
    return result
