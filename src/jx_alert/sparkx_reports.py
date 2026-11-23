"""SparkX direct daily ad report and 7-day campaign anomaly review (read-only).

Business numbers come only from SparkX `get_ads_perf`; enabled-campaign counts
come from `get_entity_metadata`. A missing row stays missing and is never shown
as zero. No SparkX write tool is ever called. Owner settings live in a private
0600 file; profile→store routes are only used after the owner saved them.
"""
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import unicodedata
from zoneinfo import ZoneInfo

import anyio
import httpx

from jx_alert.storage.job_lock import _try_file_lock, _unlock_file

BEIJING = ZoneInfo("Asia/Shanghai")
MARKETPLACE = ZoneInfo("America/Los_Angeles")
OPENS, GENERATE_UNTIL, SEND_UNTIL = time(8, 30), time(10, 0), time(12, 0)
PREFIX = "sparkx-report:"
JOB = "sparkx_report"
KINDS = ("daily", "anomaly")
ROLES = {"daily_group": "sparkx_daily_group", "daily_lead": "sparkx_daily_lead",
         "anomaly_group": "sparkx_anomaly_group", "anomaly_lead": "sparkx_anomaly_lead",
         "anomaly_operators": "sparkx_anomaly_operator"}
DESTINATIONS = {"daily_group": False, "daily_lead": True, "anomaly_group": True,
                "anomaly_lead": True, "anomaly_operators": False}
_TOOLS = frozenset({"get_user_authorized_context", "get_ads_perf", "get_entity_metadata"})
BASE_METRICS = ("Impressions", "Clicks", "Spend", "Sales", "Conversions", "Units")
_FIELDS = {"Impressions": "impressions", "Clicks": "clicks", "Spend": "spend", "Sales": "sales",
           "Conversions": "orders", "Units": "units"}
METRIC_KEYS = tuple(_FIELDS.values())
COUNTRY_LABELS = {"US": "美国", "CA": "加拿大", "MX": "墨西哥", "BR": "巴西", "UK": "英国", "GB": "英国",
                  "DE": "德国", "FR": "法国", "IT": "意大利", "ES": "西班牙", "JP": "日本", "AU": "澳大利亚",
                  "NL": "荷兰", "SE": "瑞典", "PL": "波兰", "BE": "比利时", "SG": "新加坡", "AE": "阿联酋",
                  "SA": "沙特", "IN": "印度", "TR": "土耳其", "EG": "埃及", "ZA": "南非"}
_JX_COUNTRIES = {"美国": "US", "加拿大": "CA", "墨西哥": "MX", "巴西": "BR", "英国": "UK", "德国": "DE",
                 "法国": "FR", "意大利": "IT", "西班牙": "ES", "日本": "JP", "澳大利亚": "AU", "荷兰": "NL",
                 "瑞典": "SE", "波兰": "PL", "比利时": "BE", "新加坡": "SG", "阿联酋": "AE", "沙特": "SA",
                 "印度": "IN", "土耳其": "TR"}
# Review triggers in the query currency (thresholds only apply to USD rows).
THRESHOLDS = {"zero_spend": 50.0, "zero_clicks": 10, "acos_spend": 50.0, "acos_ratio": 1.5,
              "acos_delta": 0.15, "surge_prev_spend": 30.0, "surge_spend": 50.0, "surge_up": 0.30,
              "surge_sales_down": -0.20, "stall_prev_spend": 50.0}
_PAGE_SIZE, _MAX_PAGES = 500, 20
_EMPTY_REVISION = "0" * 64
_REVISION = re.compile(r"[0-9a-f]{64}")
_SID = re.compile(r"[1-9][0-9]{0,29}")
_PID = re.compile(r"[1-9][0-9]{0,19}")
_PARTIAL_HINT = re.compile(
    r"\b(?:partial(?:_data|_result)?|incomplete(?:_data|_result)?|truncat\w*|cut[ -]?off|"
    r"not all rows|(?:first|only) \d+ rows)\b|截断|不完整|仅返回部分|仅含部分|数据不全|部分结果")


class SparkXReportError(RuntimeError):
    """A bounded public code; never contains provider text or credentials."""

    def __init__(self, code, status_code=502):
        super().__init__(code)
        self.code, self.status_code = code, status_code


class SparkXReportConfigError(RuntimeError):
    def __init__(self, detail="SparkX 日报设置无法读取，请联系管理员。", status_code=503):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


# ---------------------------------------------------------------- owner settings

def _config_path(settings):
    return Path(getattr(settings, "sparkx_report_settings_file", "/app/data/sparkx-report.json"))


def _default_config():
    return {"enabled": False, "daily_source": "lingxing", "destinations": dict(DESTINATIONS), "routes": {}}


def _valid_config(data):
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("revision"), str)
            or not _REVISION.fullmatch(data["revision"]) or type(data.get("enabled")) is not bool
            or data.get("daily_source") not in {"lingxing", "sparkx"}
            or not isinstance(data.get("destinations"), dict) or set(data["destinations"]) != set(DESTINATIONS)
            or any(type(value) is not bool for value in data["destinations"].values())
            or not isinstance(data.get("routes"), dict) or len(data["routes"]) > 1000
            or any(not isinstance(pid, str) or not _PID.fullmatch(pid)
                   or not isinstance(sid, str) or not _SID.fullmatch(sid) for pid, sid in data["routes"].items())
            or not isinstance(data.get("updated_at"), str)):
        raise ValueError
    return data


def _read_config(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise SparkXReportConfigError()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {**_default_config(), "revision": _EMPTY_REVISION, "updated_at": None}
    except OSError:
        raise SparkXReportConfigError() from None
    try:
        if len(raw) > 256 * 1024:
            raise ValueError
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError
        return _valid_config(json.loads(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        raise SparkXReportConfigError() from None


def load_config(settings):
    return _read_config(_config_path(settings))


@contextmanager
def _locked(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise SparkXReportConfigError()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise SparkXReportConfigError()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            _try_file_lock(fd)
        except OSError:
            raise SparkXReportConfigError("SparkX 日报设置正在更新，请稍后刷新。", 409) from None
        try:
            yield
        finally:
            _unlock_file(fd)
    finally:
        os.close(fd)


def save_config(settings, payload):
    """Owner save with compare-and-swap; routes are explicit profile→JX store choices."""
    fields = {"revision", "enabled", "daily_source", "destinations", "routes"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise SparkXReportConfigError("提交的 SparkX 日报设置无效。", 400)
    if not isinstance(payload["revision"], str) or not _REVISION.fullmatch(payload["revision"]):
        raise SparkXReportConfigError("页面配置版本无效，请刷新。", 400)
    routes = payload["routes"]
    if not isinstance(routes, dict):
        raise SparkXReportConfigError("店铺对应关系无效。", 400)
    clean = {}
    for pid, sid in routes.items():
        if sid in (None, ""):
            continue
        if (not isinstance(pid, str) or not _PID.fullmatch(pid)
                or not isinstance(sid, str) or not _SID.fullmatch(sid)):
            raise SparkXReportConfigError("店铺对应关系无效。", 400)
        clean[pid] = sid
    candidate = {"version": 1, "revision": secrets.token_hex(32), "enabled": payload["enabled"],
                 "daily_source": payload["daily_source"], "destinations": payload["destinations"],
                 "routes": dict(sorted(clean.items(), key=lambda item: int(item[0]))),
                 "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        _valid_config(candidate)
    except ValueError:
        raise SparkXReportConfigError("请核对开关、数据源和发送对象。", 400) from None
    path = _config_path(settings)
    try:
        with _locked(path):
            current = _read_config(path)
            if current["revision"] != payload["revision"]:
                raise SparkXReportConfigError("SparkX 日报设置已更新，请刷新后重新保存。", 409)
            fd, temporary = tempfile.mkstemp(prefix=".sparkx-report-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    if os.name != "nt":
                        os.fchmod(stream.fileno(), 0o600)
                    json.dump(candidate, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _read_config(path)["revision"] != current["revision"]:
                    raise SparkXReportConfigError("SparkX 日报设置已更新，请刷新后重新保存。", 409)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    except OSError:
        raise SparkXReportConfigError("SparkX 日报设置无法保存，请检查运行目录权限。") from None
    return candidate


def supersedes_company_digest(settings):
    """The owner chose SparkX as the 08:30 daily source and the lane can run."""
    try:
        config = load_config(settings)
    except SparkXReportConfigError:
        return False
    return config["enabled"] and config["daily_source"] == "sparkx" and _credentials(settings)[0] is not None


# ---------------------------------------------------------------- store names and routes

def short_name(name):
    """Display name in the agent style: drop '-主账号' and a trailing site code."""
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    text = re.sub(r"[-_ ]?主账号", "", text)
    text = re.sub(r"(?:[-_ ](?:US|CA|MX|BR|UK|DE|FR|IT|ES|JP|AU))+$", "", text, flags=re.IGNORECASE)
    return text.strip(" -_") or str(name or "")


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


def suggest_routes(profiles, stores):
    """Name-based profile→JX store suggestions in the same site; owner must confirm."""
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
        scored.sort(key=lambda item: (-item[0], int(item[1])))
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            result[pid] = scored[0][1]
    return result


def jx_stores(db, settings):
    """Scoped JX stores with site code and the operator label from 负责人映射."""
    from sqlalchemy import select

    from jx_alert.notifications.operators import UNMAPPED_OPERATOR, store_operator_labels
    from jx_alert.notifications.routing import _load_routing
    from jx_alert.storage.models import Store

    try:
        scope = json.loads(Path(settings.collection_scope_file).read_text(encoding="utf-8"))["store_ids"]
        ids = sorted({str(sid) for sid in scope}, key=int)
    except (OSError, ValueError, KeyError, TypeError):
        ids = []
    try:
        routing = _load_routing(settings.feishu_routing_file)
    except (OSError, RuntimeError, ValueError):
        routing = {}
    labels = store_operator_labels(routing, ids, unmapped=UNMAPPED_OPERATOR)
    rows = db.execute(select(Store.sid, Store.name, Store.country, Store.marketplace_id).where(Store.sid.in_(ids))).all()
    from jx_alert.sparkx_backend import _market
    stores = []
    for row in rows:
        code = _market(row.marketplace_id) or _JX_COUNTRIES.get(row.country or "")
        stores.append({"sid": row.sid, "name": row.name or row.sid, "country": "UK" if code == "GB" else code,
                       "operator": labels.get(row.sid, UNMAPPED_OPERATOR)})
    stores.sort(key=lambda row: (row["country"] or "", row["name"]))
    return stores, routing


def operator_routes(routing, routes):
    """profile → {'sid', 'operator', 'open_ids'} for owner-saved routes only."""
    from jx_alert.notifications.operators import UNMAPPED_OPERATOR, store_operator_labels

    stores = routing.get("stores") if isinstance(routing, dict) else {}
    stores = stores if isinstance(stores, dict) else {}
    labels = store_operator_labels(routing, set(routes.values()))
    result = {}
    for pid, sid in routes.items():
        entry = stores.get(sid)
        entry = {"open_ids": entry} if isinstance(entry, list) else entry if isinstance(entry, dict) else {}
        owners = [uid for uid in entry.get("open_ids", []) if isinstance(uid, str)
                  and re.fullmatch(r"ou_[A-Za-z0-9_-]+", uid)]
        result[pid] = {"sid": sid, "operator": labels.get(sid, UNMAPPED_OPERATOR),
                       "open_ids": list(dict.fromkeys(owners))}
    return result


# ---------------------------------------------------------------- SparkX read client

def _credentials(settings):
    from jx_alert.sparkx_config import with_console_credentials
    from jx_alert.sparkx_direct import SparkXDirectError, parse_profile_ids, validate_token

    effective = with_console_credentials(settings)
    token = getattr(effective, "sparkx_ai_token", "")
    token = token.get_secret_value() if hasattr(token, "get_secret_value") else token
    try:
        validate_token(token)
        allowed = sorted(str(pid) for pid in parse_profile_ids(effective.sparkx_profile_ids))
    except (SparkXDirectError, AttributeError, TypeError, ValueError):
        return None, []
    return token, allowed


@asynccontextmanager
async def _session(token, timeout):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from jx_alert.sparkx_direct import SPARKX_MCP_URL

    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=httpx.Timeout(timeout),
                                 follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(SPARKX_MCP_URL, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


def _reject_constant(_):
    raise ValueError("invalid JSON constant")


def _envelope(result, tool):
    """Prefer structured content; a text mirror is only used when structure is absent."""
    if getattr(result, "isError", False):
        raise SparkXReportError("SPARKX_TOOL_ERROR")
    view = result.structuredContent if isinstance(getattr(result, "structuredContent", None), dict) else None
    if view is None:
        for block in getattr(result, "content", []) or []:
            if getattr(block, "type", None) != "text":
                continue
            text = block.text.strip()
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            if text.startswith("{") and len(text) <= 4 * 1024 * 1024:
                try:
                    view = json.loads(text, parse_constant=_reject_constant)
                except (ValueError, RecursionError):
                    raise SparkXReportError("SPARKX_RESULT_UNREADABLE") from None
                break
    if not isinstance(view, dict):
        raise SparkXReportError("SPARKX_RESULT_UNREADABLE")
    if view.get("isError") is not False or "errorType" in view:
        kind = view.get("errorType")
        code = kind.upper() if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,40}", kind) else "TOOL_ERROR"
        raise SparkXReportError("SPARKX_" + code)
    if view.get("toolName", tool) != tool:
        raise SparkXReportError("SPARKX_RESULT_UNREADABLE")
    return view


async def _call(session, tool, arguments):
    if tool not in _TOOLS:
        raise SparkXReportError("SPARKX_TOOL_NOT_ALLOWED")
    return _envelope(await session.call_tool(tool, arguments), tool)


def _key(name):
    key = name.split(".", 1)[1] if "." in name else name
    return key[:-1] if key.endswith("_") else key


def _num(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise SparkXReportError("SPARKX_METRIC_INVALID")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,24}(?:\.[0-9]{1,12})?", value.strip()):
        number = float(value.strip())
    else:
        raise SparkXReportError("SPARKX_METRIC_INVALID")
    if not math.isfinite(number) or number < 0:
        raise SparkXReportError("SPARKX_METRIC_INVALID")
    return number


def _pid(value):
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, (int, str)) else ""
    return text if _PID.fullmatch(text) else None


def _day(value):
    text = str(value or "")
    if re.fullmatch(r"[0-9]{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise SparkXReportError("SPARKX_DATE_INVALID") from None


def _metrics(row):
    values = {}
    for source, target in _FIELDS.items():
        values[target] = _num(row.get(source)) if source in row else None
    if None not in (values["clicks"], values["impressions"]) and values["clicks"] > values["impressions"]:
        raise SparkXReportError("SPARKX_TRAFFIC_CONTRADICTORY")
    return values


def _page_meta(view, page, requested, *, currency_required):
    data, meta = view.get("data"), view.get("meta")
    if (not isinstance(data, dict) or not isinstance(data.get("rows"), list)
            or type(data.get("rowCount")) is not int or data["rowCount"] != len(data["rows"])
            or not isinstance(meta, dict) or type(meta.get("hasNextPage")) is not bool
            or meta.get("page", page) != page):
        raise SparkXReportError("SPARKX_PAGE_NOT_PROVEN")
    effective = meta.get("effectiveProfileIds")
    if effective is not None and (not isinstance(effective, list)
                                  or {_pid(pid) for pid in effective} != set(requested)):
        raise SparkXReportError("SPARKX_PROFILE_SCOPE_MISMATCH")
    hint = meta.get("hint")
    if isinstance(hint, str) and _PARTIAL_HINT.search(hint.casefold()):
        raise SparkXReportError("SPARKX_SOURCE_PARTIAL")
    currency = meta.get("currency")
    if currency_required and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise SparkXReportError("SPARKX_CURRENCY_UNCONFIRMED")
    return data["rows"], meta["hasNextPage"], currency


async def _pages(session, tool, arguments, requested, *, currency_required=True):
    rows, currency = [], None
    for page in range(1, _MAX_PAGES + 1):
        view = await _call(session, tool, {**arguments, "page": page, "pageSize": _PAGE_SIZE})
        batch, more, found = _page_meta(view, page, requested, currency_required=currency_required)
        if currency_required:
            if currency is not None and found != currency:
                raise SparkXReportError("SPARKX_CURRENCY_UNCONFIRMED")
            currency = found
        if not all(isinstance(row, dict) for row in batch):
            raise SparkXReportError("SPARKX_ROW_INVALID")
        rows.extend({_key(k): v for k, v in row.items()} for row in batch)
        if not more:
            return rows, currency
        if not batch:
            raise SparkXReportError("SPARKX_INCOMPLETE_PAGINATION")
        await anyio.sleep(0.2)  # Pace pages well below the 120 reads/min budget.
    raise SparkXReportError("SPARKX_PAGINATION_LIMIT")


async def _authorized_profiles(session):
    view = await _call(session, "get_user_authorized_context", {})
    rows = view.get("profiles")
    if not isinstance(rows, list):
        raise SparkXReportError("SPARKX_PROFILES_UNREADABLE")
    profiles = {}
    for row in rows[:2000]:
        pid = _pid(row.get("profileId")) if isinstance(row, dict) else None
        if pid is None or pid in profiles:
            raise SparkXReportError("SPARKX_PROFILES_UNREADABLE")
        country = row.get("countryCode")
        country = country.upper() if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country) else "未知"
        name = row.get("profileName")
        store_type = row.get("storeType") if row.get("storeType") in {"seller", "vendor"} else None
        profiles[pid] = {"name": name.strip()[:120] if isinstance(name, str) and name.strip() else pid,
                         "country": "UK" if country == "GB" else country, "store_type": store_type}
    return profiles


def report_dates(now):
    """Latest completed LA day; complete for every Americas/EU/APAC site at 08:30 Beijing."""
    latest = now.astimezone(MARKETPLACE).date() - timedelta(days=1)
    return {"report_date": latest, "previous_date": latest - timedelta(days=1),
            "window": (latest - timedelta(days=6), latest),
            "previous_window": (latest - timedelta(days=13), latest - timedelta(days=7))}


async def _collect_async(token, allowed, now, kinds, timeout):
    dates = report_dates(now)
    out = {"generated_at": now.astimezone(timezone.utc).isoformat(),
           "report_date": dates["report_date"].isoformat(), "previous_date": dates["previous_date"].isoformat(),
           "window": [day.isoformat() for day in dates["window"]],
           "previous_window": [day.isoformat() for day in dates["previous_window"]],
           "profiles": {}, "unauthorized_allowed": [], "countries": {}, "daily": {}, "total_sales": None,
           "campaigns": None, "enabled_campaigns": None, "errors": {}}
    async with _session(token, timeout) as session:
        authorized = await _authorized_profiles(session)
        out["unauthorized_allowed"] = [pid for pid in allowed if pid not in authorized]
        out["profiles"] = {pid: authorized[pid] for pid in allowed if pid in authorized}
        groups = defaultdict(list)
        for pid, profile in out["profiles"].items():
            groups[profile["country"]].append(pid)
        out["countries"] = {code: {"profiles": sorted(ids, key=int), "currency": None}
                            for code, ids in sorted(groups.items())}
        context = "JX 只读日报：店铺广告日报与近7日活动对比"
        if "daily" in kinds:
            start, end = dates["previous_date"], dates["report_date"]
            for code, info in out["countries"].items():
                ids = info["profiles"]
                rows, currency = await _pages(session, "get_ads_perf", {
                    "profileIds": [int(pid) for pid in ids], "factEntity": "campaign",
                    "dateStart": start.isoformat(), "dateEnd": end.isoformat(),
                    "metrics": list(BASE_METRICS), "select": ["date", "profile.profileId_", "profile.profileName_"],
                    "orderBy": [{"field": "profile.profileId_", "direction": "ASC"}],
                    "userContext": context, "language": "en"}, ids)
                info["currency"] = currency
                for row in rows:
                    pid, day = _pid(row.get("profileId")), _day(row.get("date"))
                    if pid not in ids or day not in (start.isoformat(), end.isoformat()):
                        raise SparkXReportError("SPARKX_ROW_SCOPE_MISMATCH")
                    if day in out["daily"].setdefault(pid, {}):
                        raise SparkXReportError("SPARKX_DUPLICATE_ROW")
                    out["daily"][pid][day] = _metrics(row)
            try:
                totals = {}
                for code, info in out["countries"].items():
                    ids = info["profiles"]
                    rows, currency = await _pages(session, "get_ads_perf", {
                        "profileIds": [int(pid) for pid in ids], "factEntity": "asin",
                        "dateStart": start.isoformat(), "dateEnd": end.isoformat(),
                        "metrics": ["TotalSalesAmount"], "select": ["date", "profile.profileId_"],
                        "userContext": context, "language": "en"}, ids)
                    if info["currency"] is not None and currency != info["currency"]:
                        raise SparkXReportError("SPARKX_CURRENCY_UNCONFIRMED")
                    for row in rows:
                        pid, day = _pid(row.get("profileId")), _day(row.get("date"))
                        if pid not in ids or day in totals.setdefault(pid, {}):
                            raise SparkXReportError("SPARKX_ROW_SCOPE_MISMATCH")
                        totals[pid][day] = _num(row.get("TotalSalesAmount"))
                out["total_sales"] = totals
            except SparkXReportError as exc:
                out["errors"]["total_sales"] = exc.code  # TACOS stays unknown, the report continues.
        if "anomaly" in kinds:
            campaigns = {}
            select = ["profile.profileId_", "campaign.campaignId_", "campaign.campaignName_",
                      "campaign.campaignType_", "campaign.campaignState_"]
            for code, info in out["countries"].items():
                ids = info["profiles"]
                for slot, (start, end) in (("w", dates["window"]), ("p", dates["previous_window"])):
                    rows, currency = await _pages(session, "get_ads_perf", {
                        "profileIds": [int(pid) for pid in ids], "factEntity": "campaign",
                        "dateStart": start.isoformat(), "dateEnd": end.isoformat(),
                        "metrics": list(BASE_METRICS), "select": select,
                        "orderBy": [{"field": "profile.profileId_", "direction": "ASC"},
                                    {"field": "campaign.campaignId_", "direction": "ASC"}],
                        "userContext": context, "language": "en"}, ids)
                    if info["currency"] is not None and currency != info["currency"]:
                        raise SparkXReportError("SPARKX_CURRENCY_UNCONFIRMED")
                    info["currency"] = currency
                    for row in rows:
                        pid, cid = _pid(row.get("profileId")), _pid(row.get("campaignId"))
                        if pid not in ids or cid is None:
                            raise SparkXReportError("SPARKX_ROW_SCOPE_MISMATCH")
                        entry = campaigns.setdefault(pid, {}).setdefault(cid, {
                            "name": None, "type": None, "state": None, "w": None, "p": None})
                        if entry[slot] is not None:
                            raise SparkXReportError("SPARKX_DUPLICATE_ROW")
                        name = row.get("campaignName")
                        entry["name"] = entry["name"] or (name.strip()[:200] if isinstance(name, str) and name.strip()
                                                          else cid)
                        entry["type"] = entry["type"] or (row.get("campaignType") if isinstance(
                            row.get("campaignType"), str) else None)
                        if isinstance(row.get("campaignState"), str):
                            entry["state"] = row["campaignState"].strip().lower()
                        entry[slot] = _metrics(row)
            out["campaigns"] = campaigns
            idle = [pid for pid in out["profiles"] if not any(
                ((item["w"] or {}).get("impressions") or 0) > 0 or ((item["p"] or {}).get("impressions") or 0) > 0
                for item in campaigns.get(pid, {}).values())]
            if idle:
                try:
                    counts = {pid: 0 for pid in idle}
                    for code, info in out["countries"].items():
                        ids = [pid for pid in info["profiles"] if pid in counts]
                        if not ids:
                            continue
                        rows, _ = await _pages(session, "get_entity_metadata", {
                            "entity": "campaign", "profileIds": [int(pid) for pid in ids],
                            "filters": {"campaignState": "enabled"}, "select": ["profileId", "campaignState"],
                            "userContext": context}, ids, currency_required=False)
                        for row in rows:
                            pid = _pid(row.get("profileId"))
                            if pid not in counts:
                                raise SparkXReportError("SPARKX_ROW_SCOPE_MISMATCH")
                            counts[pid] += 1
                    out["enabled_campaigns"] = counts
                except SparkXReportError as exc:
                    out["errors"]["enabled_campaigns"] = exc.code
            else:
                out["enabled_campaigns"] = {}
    return out


def _unwrap(error):
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, SparkXReportError):
            return item
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        pending.extend((item.__cause__, item.__context__))
    return None


def collect(settings, *, now, kinds=KINDS, timeout=30.0, total_timeout=240.0):
    """Run one read-only SparkX session. Raises SparkXReportError with a safe code."""
    from jx_alert.sparkx_direct import failure_code

    token, allowed = _credentials(settings)
    if token is None:
        raise SparkXReportError("SPARKX_NOT_CONFIGURED", 409)
    if not allowed:
        raise SparkXReportError("SPARKX_NO_PROFILES", 409)

    async def run():
        with anyio.fail_after(total_timeout):
            return await _collect_async(token, allowed, now, set(kinds), timeout)

    try:
        return anyio.run(run)
    except SparkXReportError:
        raise
    except BaseException as exc:  # noqa: BLE001 - MCP wraps failures in exception groups.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        found = _unwrap(exc)
        if found is not None:
            raise found from None
        raise SparkXReportError("SPARKX_" + failure_code(exc)) from None


def list_profiles(settings, *, timeout=20.0):
    """Authorized profiles only (no metrics), for the owner mapping table."""
    from jx_alert.sparkx_direct import failure_code

    token, allowed = _credentials(settings)
    if token is None:
        raise SparkXReportError("SPARKX_NOT_CONFIGURED", 409)

    async def run():
        with anyio.fail_after(timeout + 10):
            async with _session(token, timeout) as session:
                return await _authorized_profiles(session)

    try:
        profiles = anyio.run(run)
    except SparkXReportError:
        raise
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        found = _unwrap(exc)
        if found is not None:
            raise found from None
        raise SparkXReportError("SPARKX_" + failure_code(exc)) from None
    return profiles, allowed


# ---------------------------------------------------------------- deterministic analysis

def without_profiles(collected, excluded):
    """Copy of collected data without the given profiles (e.g. stores whose notices are paused)."""
    if not excluded:
        return collected
    keep = {pid: row for pid, row in collected["profiles"].items() if pid not in excluded}
    countries = {}
    for code, info in collected["countries"].items():
        ids = [pid for pid in info["profiles"] if pid in keep]
        if ids:
            countries[code] = {**info, "profiles": ids}

    def strip(mapping):
        return None if mapping is None else {pid: value for pid, value in mapping.items() if pid in keep}

    return {**collected, "profiles": keep, "countries": countries, "daily": strip(collected["daily"]),
            "total_sales": strip(collected.get("total_sales")), "campaigns": strip(collected.get("campaigns")),
            "enabled_campaigns": strip(collected.get("enabled_campaigns"))}


def _sum(rows, key):
    values = [row.get(key) for row in rows if row is not None]
    if not values or any(value is None for value in values):
        return None
    return math.fsum(values)


def _totals(rows):
    rows = [row for row in rows if row is not None]
    return {key: _sum(rows, key) for key in METRIC_KEYS} if rows else None


def ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def change(current, previous):
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous


def _names(profiles):
    """Short display names (non-US keep their site), falling back to full names on collision."""
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


def _country_order(collected, spend):
    return sorted(collected["countries"], key=lambda code: (code != "US", -(spend.get(code) or 0), code))


def build_daily(collected, people):
    names = _names(collected["profiles"])
    day, before = collected["report_date"], collected["previous_date"]
    daily, totals = collected["daily"], collected.get("total_sales")
    sites, spend_by_site = [], {}
    for code, info in collected["countries"].items():
        ids = info["profiles"]
        today = {pid: daily.get(pid, {}).get(day) for pid in ids}
        prior = {pid: daily.get(pid, {}).get(before) for pid in ids}
        current, previous = _totals(today.values()), _totals(prior.values())
        spenders = sorted((pid for pid in ids if today[pid] and (today[pid]["spend"] or 0) > 0),
                          key=lambda pid: -(today[pid]["spend"] or 0))
        total_sales = None
        if totals is not None:
            found = [totals.get(pid, {}).get(day) for pid in ids if totals.get(pid, {}).get(day) is not None]
            total_sales = math.fsum(found) if found else None
        spend_by_site[code] = (current or {}).get("spend")
        stores = [{"pid": pid, "name": names[pid], "operator": people.get(pid),
                   "today": today[pid], "prior": prior[pid],
                   "spend_change": change(today[pid]["spend"], (prior[pid] or {}).get("spend"))}
                  for pid in spenders]
        quiet = [names[pid] for pid in ids if pid not in spenders]
        sites.append({"code": code, "label": COUNTRY_LABELS.get(code, code), "currency": info["currency"],
                      "store_count": len(ids), "with_data": len(spenders), "current": current,
                      "previous": previous, "total_sales": total_sales, "stores": stores, "quiet": quiet,
                      "empty": not any(today.values())})
    order = _country_order(collected, spend_by_site)
    sites.sort(key=lambda site: order.index(site["code"]))
    usd = [site for site in sites if site["currency"] == "USD" and site["current"]]
    total = None
    if len(usd) > 1:
        total = {"current": _totals([site["current"] for site in usd]),
                 "previous": _totals([site["previous"] for site in usd if site["previous"]]),
                 "store_count": sum(site["store_count"] for site in usd),
                 "with_data": sum(site["with_data"] for site in usd)}
    return {"report_date": day, "previous_date": before, "sites": sites, "total": total,
            "notes": _daily_notes(collected, sites), "profile_count": len(collected["profiles"]),
            "with_data": sum(site["with_data"] for site in sites)}


def money(value, currency):
    if value is None:
        return "—"
    if currency == "USD":
        return f"${value:,.0f}" if abs(value) >= 100 else f"${value:,.2f}"
    return f"{value:,.0f} {currency or ''}".strip() if abs(value) >= 100 else f"{value:,.2f} {currency or ''}".strip()


def percent(value, digits=1):
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def signed(value):
    return "—" if value is None else f"{value * 100:+.0f}%"


def _daily_notes(collected, sites):
    notes = []
    empty = [site for site in sites if site["empty"] and site["store_count"]]
    if empty:
        stores = sum(site["store_count"] for site in empty)
        labels = "、".join(site["label"] for site in empty)
        notes.append(f"{labels}共 {stores} 个店铺，SparkX 当日没有返回任何广告数据（不是 0，而是查不到记录）。"
                     "如果这些站点确实在投放，最可能是 Token 的店铺范围只勾选了部分站点；"
                     "请到 SparkX 后台「MCP & Skills」核对该 Token 的授权范围。")
    main = next((site for site in sites if site["stores"]), None)
    if main:
        cur, current = main["currency"], main["current"] or {}
        acos = ratio(current.get("spend"), current.get("sales"))
        stores = [row for row in main["stores"] if (row["today"]["spend"] or 0) >= THRESHOLDS["zero_spend"]]
        acos_rows = [(ratio(row["today"]["spend"], row["today"]["sales"]), row) for row in stores
                     if (row["today"]["sales"] or 0) > 0]
        acos_rows = [(value, row) for value, row in acos_rows if value is not None]
        spread = (max(v for v, _ in acos_rows) - min(v for v, _ in acos_rows)) if len(acos_rows) > 1 else 0
        parts = [f"{main['label']}站 ACOS {percent(acos)}" + ("，店铺间分化较大：" if spread >= 0.2 else "。")]
        top = main["stores"][0]
        share = ratio(top["today"]["spend"], current.get("spend"))
        sales_share = ratio(top["today"]["sales"], current.get("sales"))
        if share is not None and share >= 0.25:
            parts.append(f"{top['name']} 一家店占花费的 {percent(share, 0)}（{money(top['today']['spend'], cur)}），"
                         f"销售额占 {percent(sales_share, 0)}（{money(top['today']['sales'], cur)}），"
                         f"ACOS {percent(ratio(top['today']['spend'], top['today']['sales']))}。")
        ranked = sorted(acos_rows, key=lambda item: -item[0])
        for index, (value, row) in enumerate(ranked[:2]):
            if value >= 0.4:
                parts.append(f"{row['name']} 花费 {money(row['today']['spend'], cur)}，只带回 "
                             f"{money(row['today']['sales'], cur)}，ACOS {percent(value)}"
                             + ("，为全站最高。" if index == 0 else "，排名第二高。"))
        for row in stores:
            if row["today"]["sales"] == 0:
                clicks = row["today"]["clicks"]
                parts.append(f"{row['name']} 花费 {money(row['today']['spend'], cur)}，销售额为 0"
                             + (f"，{clicks:,.0f} 次点击没有出单。" if clicks is not None else "。"))
        if len(ranked) > 1 and ranked[-1][1]["pid"] != top["pid"]:
            value, row = ranked[-1]
            parts.append(f"{row['name']} ACOS {percent(value)}（花费 {money(row['today']['spend'], cur)}），"
                         "是全站效率最好的。")
        notes.append("\n".join(parts))
    if collected.get("unauthorized_allowed"):
        notes.append(f"{len(collected['unauthorized_allowed'])} 个已允许的 Profile 不在当前 Token 授权内，已跳过。")
    if collected["errors"].get("total_sales"):
        notes.append("SparkX 未返回店铺总销售额，TACOS 暂不显示。")
    notes.append("销售额、订单为 SparkX 截至生成时的归因数据，近 7–14 天仍会回补；花费与点击基本已定。"
                 "TACOS=广告花费÷SparkX 店铺总销售额；多店查询的金额由 SparkX 统一折算为 USD。")
    return notes


def _classify(item, usd):
    w, p = item["w"] or {}, item["p"] or {}
    ws, wc, wo, wsl = w.get("spend"), w.get("clicks"), w.get("orders"), w.get("sales")
    ps, psl = p.get("spend"), p.get("sales")
    t = THRESHOLDS
    if not usd:
        return None
    if item["state"] == "enabled" and ps is not None and ps >= t["stall_prev_spend"] and (
            not item["w"] or w.get("impressions") == 0):
        return "stalled"
    up = change(ws, ps)
    if ws is not None and ws >= t["zero_spend"] and wc is not None and wc >= t["zero_clicks"] and wo == 0:
        if psl and up is not None and up >= t["surge_up"]:
            return "surge"
        return "zero_orders"
    down = change(wsl, psl)
    if (ws is not None and ws >= t["surge_spend"] and ps is not None and ps >= t["surge_prev_spend"]
            and up is not None and up >= t["surge_up"] and down is not None and down <= t["surge_sales_down"]):
        return "surge"
    acos_w, acos_p = ratio(ws, wsl), ratio(ps, psl)
    if (ws is not None and ws >= t["acos_spend"] and acos_w is not None and acos_p is not None
            and acos_w >= acos_p * t["acos_ratio"] and acos_w - acos_p >= t["acos_delta"]):
        return "acos_worse"
    return None


CATEGORY_LABELS = {"stalled": "停摆", "zero_orders": "零出单", "surge": "加预算后销售下滑", "acos_worse": "ACOS 恶化"}
_CATEGORY_ORDER = ("stalled", "zero_orders", "surge", "acos_worse")


def build_anomalies(collected, people):
    names = _names(collected["profiles"])
    campaigns = collected.get("campaigns") or {}
    enabled = collected.get("enabled_campaigns")
    sites, findings, zero_stores, idle_stores, stores = [], [], [], [], {}
    for code, info in collected["countries"].items():
        usd = info["currency"] == "USD"
        ids = info["profiles"]
        w_rows, p_rows, active = [], [], 0
        for pid in ids:
            items = campaigns.get(pid, {})
            w_rows += [item["w"] for item in items.values() if item["w"]]
            p_rows += [item["p"] for item in items.values() if item["p"]]
            delivering = any(((item["w"] or {}).get("impressions") or 0) > 0 for item in items.values())
            active += delivering
            touched = delivering or any(((item["p"] or {}).get("impressions") or 0) > 0 for item in items.values())
            if not touched:
                count = None if enabled is None else enabled.get(pid)
                row = {"pid": pid, "name": names[pid], "operator": people.get(pid), "enabled": count,
                       "country": code}
                (zero_stores if count else idle_stores).append(row)
                continue
            store_w = _totals([item["w"] for item in items.values() if item["w"]])
            store_p = _totals([item["p"] for item in items.values() if item["p"]])
            for cid, item in items.items():
                category = _classify(item, usd)
                if category:
                    findings.append({"pid": pid, "store": names[pid], "operator": people.get(pid), "cid": cid,
                                     "campaign": item["name"], "type": item["type"], "category": category,
                                     "w": item["w"], "p": item["p"], "currency": info["currency"]})
            stores[pid] = {"pid": pid, "name": names[pid], "operator": people.get(pid), "w": store_w,
                           "p": store_p, "currency": info["currency"]}
        sites.append({"code": code, "label": COUNTRY_LABELS.get(code, code), "currency": info["currency"],
                      "store_count": len(ids), "active": active, "w": _totals(w_rows), "p": _totals(p_rows)})
    by_store = defaultdict(list)
    for item in findings:
        by_store[item["pid"]].append(item)
    overview = []
    for pid, items in by_store.items():
        store = stores[pid]
        counts = defaultdict(int)
        for item in items:
            counts[item["category"]] += 1
        trend = []
        spend_change = change((store["w"] or {}).get("spend"), (store["p"] or {}).get("spend"))
        sales_change = change((store["w"] or {}).get("sales"), (store["p"] or {}).get("sales"))
        if (sales_change is not None and sales_change <= -0.2) or (spend_change is not None and spend_change >= 0.3):
            trend.append(f"花费 {signed(spend_change)}、销售 {signed(sales_change)}")
        summary = "、".join(f"{counts[code]} 个" + (" " if CATEGORY_LABELS[code][0].isascii() else "")
                           + CATEGORY_LABELS[code] for code in _CATEGORY_ORDER if counts[code])
        overview.append({**store, "count": len(items), "problem": "；".join([*trend, summary]),
                         "acos_w": ratio((store["w"] or {}).get("spend"), (store["w"] or {}).get("sales")),
                         "acos_p": ratio((store["p"] or {}).get("spend"), (store["p"] or {}).get("sales"))})
    overview.sort(key=lambda row: (-row["count"], -((row["w"] or {}).get("spend") or 0)))
    grouped = {code: [] for code in _CATEGORY_ORDER}
    for item in findings:
        grouped[item["category"]].append(item)
    for code, items in grouped.items():
        items.sort(key=lambda item, code=code: -(((item["p"] if code == "stalled" else item["w"]) or {})
                                                 .get("spend") or 0))
    return {"window": collected["window"], "previous_window": collected["previous_window"], "sites": sites,
            "findings": findings, "grouped": grouped, "overview": overview, "zero_stores": zero_stores,
            "idle_stores": idle_stores, "store_total": len(collected["profiles"]),
            "active_total": sum(site["active"] for site in sites),
            "metadata_unavailable": enabled is None,
            "suggestions": _suggestions(grouped, overview, zero_stores)}


def _campaign_label(name, limit=40):
    text = str(name or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _suggestions(grouped, overview, zero_stores):
    lines = []
    if zero_stores:
        names = "、".join(row["name"] for row in zero_stores[:3])
        lines.append(f"{names} 先查账户状态（余额/信用卡、店铺是否暂停、Listing 是否下架、预算是否清零），"
                     "这是“全店停投”，损失最大。")
    worse = defaultdict(list)
    for item in grouped["acos_worse"]:
        worse[item["pid"]].append(item)
    for pid, items in sorted(worse.items(), key=lambda kv: -sum((i["w"] or {}).get("spend") or 0 for i in kv[1]))[:2]:
        first = items[0]
        acos = (percent(ratio(first["p"]["spend"], first["p"]["sales"]), 0) + "→"
                + percent(ratio(first["w"]["spend"], first["w"]["sales"]), 0))
        store = next((row for row in overview if row["pid"] == pid), None)
        tail = f"，店铺近 7 日 ACOS {percent(store['acos_w'], 0)}" if store and store["acos_w"] is not None else ""
        lines.append(f"{first['store']} 的 {len(items)} 个 ACOS 恶化活动（如 {_campaign_label(first['campaign'], 30)}"
                     f" {acos}）建议复核出价和广告位溢价{tail}，别被这几个拖回去。")
    for item in grouped["zero_orders"][:2]:
        w = item["w"]
        test = re.search(r"test|测试", item["campaign"] or "", re.IGNORECASE)
        lines.append(f"{item['store']} {_campaign_label(item['campaign'], 36)}：{int(w['clicks'])} 次点击零转化、"
                     f"花费 {money(w['spend'], item['currency'])}，建议排查搜索词，否词或降出价"
                     + ("；名称含 TEST/测试，可考虑直接归档。" if test else "。"))
    for item in grouped["surge"][:1]:
        lines.append(f"{item['store']} {_campaign_label(item['campaign'], 36)} 花费 "
                     f"{signed(change(item['w']['spend'], item['p']['spend']))}、销售 "
                     f"{signed(change(item['w']['sales'], item['p']['sales']))}，放量没有带来销售，建议收回新增预算。")
    for item in grouped["stalled"][:1]:
        lines.append(f"{item['store']} {_campaign_label(item['campaign'], 36)} 前 7 日花费 "
                     f"{money(item['p']['spend'], item['currency'])}、近 7 日无曝光：查预算是否改为 0、是否断货或被限流。")
    return lines[:5]


# ---------------------------------------------------------------- delivery planning

def is_report(key, payload):
    return key.startswith(PREFIX) and payload.get("sparkx_report") in KINDS


def delivery_window(key, payload, now):
    instant = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    day = payload.get("sparkx_report_day")
    try:
        scheduled = date.fromisoformat(day)
        if not key.startswith(f"{PREFIX}{payload.get('sparkx_report')}:{scheduled.isoformat()}:"):
            raise ValueError
    except (TypeError, ValueError):
        return "EXPIRED", None
    local = instant.astimezone(BEIJING)
    opens = datetime.combine(scheduled, OPENS, BEIJING)
    if local < opens:
        return "WAITING", opens.astimezone(timezone.utc).replace(tzinfo=None)
    if local.date() != scheduled or local.time() >= SEND_UNTIL:
        return "EXPIRED", None
    return "READY", None


def _key_for(kind, day, delivery):
    digest = hashlib.sha256(json.dumps([delivery["role"], delivery["receive_id"]]).encode()).hexdigest()[:24]
    return f"{PREFIX}{kind}:{day}:{digest}"


def _daily_lead(settings, routing):
    from jx_alert.notifications.feishu_app import FeishuAppClient, FeishuAppError

    binding = routing.get("company_executive_digest") if isinstance(routing, dict) else None
    if not isinstance(binding, dict):
        return None
    uid, app_id, generation = binding.get("open_id"), binding.get("app_id"), binding.get("generation")
    if (not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,150}", uid)
            or not isinstance(app_id, str) or not isinstance(generation, str)):
        return None
    try:
        if FeishuAppClient.configured_app_identity(settings) != hashlib.sha256(app_id.encode()).hexdigest():
            return None
    except (FeishuAppError, OSError, TypeError, ValueError):
        return None
    return {"role": ROLES["daily_lead"], "receive_id_type": "open_id", "receive_id": uid, "open_ids": [],
            "generation": generation}


def _anomaly_lead(settings):
    from jx_alert.notifications.recipient_policy import advertising_subscriber_deliveries

    leads = advertising_subscriber_deliveries(settings)
    if len(leads) != 1:
        return None
    return {"role": ROLES["anomaly_lead"], "receive_id_type": "open_id", "receive_id": leads[0]["receive_id"],
            "open_ids": [], "generation": leads[0]["subscription_generation"]}


def _group(settings, routing, kind):
    from jx_alert.notifications.routing import ApplicationRoutingError, report_group_delivery

    try:
        return report_group_delivery(settings, routing, role=ROLES[kind + "_group"])
    except ApplicationRoutingError:
        return None


def _members(routing, settings):
    groups = routing.get("groups", {}) if isinstance(routing, dict) else {}
    group = groups.get(getattr(settings, "feishu_daily_report_chat_id", "")) or {}
    roster = group.get("verified_member_open_ids")
    return None if roster is None else set(roster)


def _recent_run(db, day):
    from sqlalchemy import select

    from jx_alert.storage.models import SyncJob

    jobs = list(db.scalars(select(SyncJob).where(
        SyncJob.job_type == JOB, SyncJob.metadata_json["day"].as_string() == day).order_by(SyncJob.id.desc())))
    return jobs


def queue_reports(db, settings, *, now, collected=None):
    """Collect once, freeze cards, enqueue one message per destination and day."""
    from sqlalchemy import select

    from jx_alert.notifications import sparkx_report_cards as cards
    from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification
    from jx_alert.notifications.routing import _load_routing
    from jx_alert.storage.models import SyncJob
    from jx_alert.store_controls import notification_states

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("SparkX report requires a timezone-aware clock")
    config = load_config(settings)
    if not config["enabled"]:
        return {"status": "DISABLED"}
    if (settings.app_dry_run or not settings.feishu_enabled
            or getattr(settings, "feishu_transport", "webhook") != "app"):
        return {"status": "DELIVERY_DISABLED"}
    local = now.astimezone(BEIJING)
    if not OPENS <= local.time() < GENERATE_UNTIL:
        return {"status": "WAITING"}
    day = local.date().isoformat()
    jobs = _recent_run(db, day)
    if any(job.status == "SUCCEEDED" for job in jobs):
        return {"status": "ALREADY_COMPLETE"}
    failures = [job for job in jobs if job.status == "FAILED"]
    instant = now.astimezone(timezone.utc).replace(tzinfo=None)
    if collected is None and (len(failures) >= 6 or failures and failures[0].started_at
                              and instant - failures[0].started_at < timedelta(minutes=10)):
        return {"status": "RETRY_LATER"}
    routing = _load_routing(settings.feishu_routing_file)
    wanted = config["destinations"]
    plans = []
    if wanted["daily_group"] and (group := _group(settings, routing, "daily")):
        plans.append(("daily", group))
    if wanted["daily_lead"] and (lead := _daily_lead(settings, routing)):
        plans.append(("daily", lead))
    if wanted["anomaly_group"] and (group := _group(settings, routing, "anomaly")):
        plans.append(("anomaly", group))
    if wanted["anomaly_lead"] and (lead := _anomaly_lead(settings)):
        plans.append(("anomaly", lead))
    kinds = {kind for kind, _ in plans} | ({"anomaly"} if wanted["anomaly_operators"] else set())
    if not kinds:
        db.add(SyncJob(job_type=JOB, status="SKIPPED", started_at=instant,
                       metadata_json={"day": day, "reason": "NO_RECIPIENT"}))
        return {"status": "NO_RECIPIENT"}
    try:
        data = collected if collected is not None else collect(settings, now=now, kinds=kinds)
    except SparkXReportError as exc:
        db.add(SyncJob(job_type=JOB, status="FAILED", started_at=instant,
                       metadata_json={"day": day, "error": exc.code}))
        return {"status": "FAILED", "error": exc.code}
    routes = operator_routes(routing, {pid: sid for pid, sid in config["routes"].items()
                                       if pid in data["profiles"]})
    # The owner's per-store notification switch also applies to SparkX data of that store.
    paused = {sid for sid, (on, _) in notification_states(settings, {r["sid"] for r in routes.values()}).items()
              if not on}
    data = without_profiles(data, {pid for pid, route in routes.items() if route["sid"] in paused})
    routes = {pid: route for pid, route in routes.items() if pid in data["profiles"]}
    people = {pid: route["operator"] for pid, route in routes.items()}
    daily = build_daily(data, people) if "daily" in kinds else None
    anomaly = build_anomalies(data, people) if "anomaly" in kinds else None
    members = _members(routing, settings)

    def allowed(uid):
        return members is None or uid in members

    mentions = list(dict.fromkeys(uid for item in (anomaly or {}).get("findings", []) + (anomaly or {}).get("zero_stores", [])
                                  for uid in routes.get(item["pid"], {}).get("open_ids", []) if allowed(uid)))
    leads = {delivery["receive_id"] for kind, delivery in plans if kind == "anomaly"}
    if wanted["anomaly_operators"] and anomaly:
        owners = defaultdict(set)
        for item in anomaly["findings"] + anomaly["zero_stores"]:
            for uid in routes.get(item["pid"], {}).get("open_ids", []):
                if allowed(uid) and uid not in leads:
                    owners[uid].add(item["pid"])
        for uid, pids in sorted(owners.items()):
            plans.append(("anomaly", {"role": ROLES["anomaly_operators"], "receive_id_type": "open_id",
                                      "receive_id": uid, "open_ids": [], "profiles": sorted(pids, key=int)}))
    ids, skipped = [], 0
    for kind, delivery in plans:
        key = _key_for(kind, day, delivery)
        scope = delivery.get("profiles")
        if kind == "daily":
            value = cards.daily_card(daily)
            shown = list(data["profiles"])
        else:
            if scope is not None:
                value = cards.anomaly_card(anomaly, only=set(scope))
                shown = scope
            else:
                value = cards.anomaly_card(anomaly, mentions=mentions if delivery["receive_id_type"] == "chat_id"
                                           else None)
                shown = list(data["profiles"])
        if value is None:
            skipped += 1
            continue
        store_ids = sorted({routes[pid]["sid"] for pid in shown if pid in routes}, key=int)
        if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key)) is not None:
            continue  # Never rewrite a frozen card for the same day and recipient.
        row = enqueue_notification(db, idempotency_key=key, text=value["header"]["title"]["content"],
                                   chat_id=delivery["receive_id"], delivery=delivery, card=value,
                                   fixed_route=True, store_ids=store_ids)
        row.payload = {**row.payload, "sparkx_report": kind, "sparkx_report_day": day,
                       "sparkx_report_date": data["report_date"], "sparkx_profiles": sorted(shown, key=int)}
        db.flush()
        ids.append(row.id)
    db.add(SyncJob(job_type=JOB, status="SUCCEEDED", started_at=instant, metadata_json={
        "day": day, "report_date": data["report_date"], "outbox_ids": ids, "skipped": skipped,
        "profiles": len(data["profiles"]), "findings": len((anomaly or {}).get("findings", [])),
        "errors": data["errors"]}))
    return {"status": "QUEUED", "outbox_ids": ids}


def delivery_blocked(settings, key, payload, created_at, now):
    """Recheck the owner's switches and the exact recipient before each send attempt."""
    from jx_alert.notifications.routing import _load_routing

    try:
        config = load_config(settings)
        if not config["enabled"]:
            return "SparkX 日报已停用，保留记录不补发"
        if delivery_window(key, payload, now)[0] != "READY":
            return "SparkX 日报已过发送时段，保留记录"
        delivery = payload.get("delivery") or {}
        role = delivery.get("role")
        kind = payload["sparkx_report"]
        flag = next((name for name, value in ROLES.items() if value == role), None)
        if flag is None or not flag.startswith(kind) or not config["destinations"][flag]:
            return "SparkX 日报发送对象已关闭，保留记录"
        routing = _load_routing(settings.feishu_routing_file)
        if role in {ROLES["daily_group"], ROLES["anomaly_group"]}:
            current = _group(settings, routing, kind)
            if current is None or current["receive_id"] != delivery.get("receive_id"):
                return "通知群已更换，SparkX 日报保留记录"
        elif role == ROLES["daily_lead"]:
            current = _daily_lead(settings, routing)
            if current is None or current != delivery:
                return "公司日报总负责人已变更，保留记录"
        elif role == ROLES["anomaly_lead"]:
            current = _anomaly_lead(settings)
            if current is None or current != delivery:
                return "广告总负责人已变更，保留记录"
        else:
            routes = operator_routes(routing, config["routes"])
            members = _members(routing, settings)
            uid = delivery.get("receive_id")
            if (members is not None and uid not in members) or not delivery.get("profiles") or any(
                    uid not in routes.get(pid, {}).get("open_ids", []) for pid in delivery["profiles"]):
                return "店铺对应的运营已变更，保留记录"
    except (SparkXReportConfigError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        return "SparkX 日报设置无法核验，保留记录"
    return None


def preview(settings, kind, *, now, db=None):
    """Live, read-only: the card the lane would freeze now for the group/lead view."""
    from jx_alert.notifications import sparkx_report_cards as cards
    from jx_alert.notifications.routing import _load_routing

    if kind not in KINDS:
        raise SparkXReportError("SPARKX_KIND_INVALID", 400)
    config = load_config(settings)
    data = collect(settings, now=now, kinds={kind})
    try:
        routing = _load_routing(settings.feishu_routing_file)
    except (OSError, RuntimeError, ValueError):
        routing = {}
    routes = operator_routes(routing, {pid: sid for pid, sid in config["routes"].items() if pid in data["profiles"]})
    people = {pid: route["operator"] for pid, route in routes.items()}
    if kind == "daily":
        return cards.daily_card(build_daily(data, people)), data
    return cards.anomaly_card(build_anomalies(data, people)), data
