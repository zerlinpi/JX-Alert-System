"""Isolated, fixed-origin SparkX campaign observations for a selected JX store.

No campaign, DailyMetric, coverage, incident or outbox mutation is performed.
Exact proof and pagination are required before returning a usable observation.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from sqlalchemy import select

from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.sparkx_config import with_console_credentials
from jx_alert.sparkx_direct import (
    SparkXDirectError, call_sparkx, parse_profile_ids, validate_performance_query, validate_token,
)
from jx_alert.storage.models import RawSnapshot


SOURCES = {"daily": "sparkx_backend_campaign_daily", "hourly": "sparkx_backend_campaign_hourly"}
SCHEMA = "sparkx-single-profile-sp-campaign-v1"
DAILY_METRICS = ("Spend", "Impressions", "Clicks", "Conversions", "Sales", "Units")
HOURLY_METRICS = ("Spend", "Impressions", "Clicks")  # Do not promote AMS conversion figures.
_SP = "sponsoredProducts"
_MARKETPLACE_CODES = {
    "ATVPDKIKX0DER": "US", "A2EUQ1WTGCTBG2": "CA", "A1AM78C64UM0Y8": "MX",
    "A1PA6795UKMFR9": "DE", "A1F83G8C2ARO7P": "GB", "A13V1IB3VIYZZH": "FR",
    "APJ6JRA9NG5V4": "IT", "A1RKKUPIHCS9HS": "ES", "A1VC38T7YXB528": "JP",
    "A39IBJ37TRP1C6": "AU", "A21TJRUUN4KGV": "IN", "A2Q3Y263D00KWC": "BR",
    "A1805IZSGTT6HS": "NL", "A2NODRKZP88ZB9": "SE", "A1C3SOZRARQ6R3": "PL",
    "A1HUBM43NQA1VQ": "AE", "A17E79C6D8DWNP": "SA", "A19VAU5U5O7RUS": "SG",
    "A33AVAJ2PDY3EV": "TR", "ARBP9OOSHTCHU": "EG", "AMEN7PMS3EDWL": "BE",
}
_COUNTRY_LABELS = {"美国": "US", "加拿大": "CA", "墨西哥": "MX", "英国": "GB", "日本": "JP",
                   "德国": "DE", "法国": "FR", "意大利": "IT", "西班牙": "ES", "澳大利亚": "AU",
                   "印度": "IN", "巴西": "BR", "美国站": "US", "英国站": "GB", "日本站": "JP",
                   "United States": "US", "United Kingdom": "GB", "Japan": "JP"}
_PAGE_SIZE = 200
_MAX_PAGES = 12
# SparkX documents a shared interpretation suffix on successful responses.
# Strip only that advice, not arbitrary clauses claiming the source is partial.
_GENERIC_HINT_PARTS = (
    "limit conclusions to this query's scope.",
    "missing fields are not zero, and empty results do not prove that no matching business data exists.",
    "do not present partial or paginated results as complete.",
    "distinguish returned facts from analytical inferences; state limitations or query further when evidence is insufficient.",
)
_PARTIAL_HINT = re.compile(
    r"\b(?:partial(?:_data|_result)?|incomplete(?:_data|_result)?|truncat\w*|cut[ -]?off|data[ _-](?:missing|limited)|"
    r"not all rows|(?:first|only) \d+ rows)\b|截断|不完整|仅返回部分|仅含部分|数据不全|部分结果"
)


class SparkXBackendError(SparkXDirectError):
    """A bounded public reason that never includes provider content or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _numeric_id(value):
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        value = int(value)
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def _market(value):
    if not isinstance(value, str):
        return None
    val = value.strip()
    if val in _COUNTRY_LABELS:
        return _COUNTRY_LABELS[val]
    val = val.upper()
    return "GB" if val == "UK" else _MARKETPLACE_CODES.get(val, val if re.fullmatch(r"[A-Z]{2}", val) else None)


def _clock(value):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SparkXBackendError("INVALID_CLOCK")
    return value.astimezone(timezone.utc)


def _scope(settings, store, binding):
    settings = with_console_credentials(settings)
    if (store is None or getattr(store, "status", None) != 1 or not isinstance(binding, dict)
            or not re.fullmatch(r"[1-9][0-9]{0,29}", str(getattr(store, "sid", "")))):
        raise SparkXBackendError("STORE_NOT_AUTHORIZED")
    profile = _numeric_id(binding.get("profile_id"))
    marketplace_id = binding.get("marketplace")
    country = _market(marketplace_id)
    configured_country = _market(getattr(store, "country", None))
    raw_marketplace = getattr(store, "marketplace_id", None)
    store_marketplace = _market(raw_marketplace)
    currency = binding.get("currency")
    zone = binding.get("timezone")
    if (not profile or not country or not isinstance(marketplace_id, str)
            or marketplace_id != raw_marketplace or not store_marketplace
            or any(found != country for found in (configured_country, store_marketplace) if found)
            or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)
            or currency != getattr(store, "currency_code", None) or zone != getattr(store, "timezone", None)):
        raise SparkXBackendError("STORE_BINDING_MISMATCH")
    try:
        ZoneInfo(zone)
        permitted = parse_profile_ids(settings.sparkx_profile_ids)
        token = settings.sparkx_ai_token
        token = token.get_secret_value() if hasattr(token, "get_secret_value") else token
        validate_token(token)
    except (AttributeError, SparkXDirectError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise SparkXBackendError("SPARKX_CREDENTIALS_UNAVAILABLE") from None
    if profile not in permitted:
        raise SparkXBackendError("SPARKX_PROFILE_NOT_ALLOWED")
    return {"profile_id": profile, "marketplace": marketplace_id, "country": country,
            "currency": currency, "timezone": zone}, token


def _invalid_constant(_):
    raise ValueError("invalid JSON constant")


def _envelope(raw, tool):
    if raw.isError:
        raise SparkXBackendError("SPARKX_READ_REJECTED")
    try:
        views = [raw.structuredContent] if raw.structuredContent is not None else []
        for block in raw.content:
            if block.type != "text":
                continue
            text = block.text.strip()
            if len(text) > 512 * 1024:
                raise ValueError
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            if text.startswith(("{", "[")):
                # Numeric identity in the SDK's structured packet is already decoded.
                # A text mirror must agree; do not choose a contradictory view.
                views.append(json.loads(text, object_pairs_hook=unique_json_fields,
                                        parse_constant=_invalid_constant))
        # JSON serialization also distinguishes true from 1 (Python equality does not).
        canonical = [json.dumps(view, allow_nan=False, sort_keys=True, separators=(",", ":"))
                     for view in views if isinstance(view, dict)]
        if (not views or len(canonical) != len(views) or len(set(canonical)) != 1
                or views[0].get("isError") is not False or views[0].get("toolName") != tool
                or "errorType" in views[0]):
            raise ValueError
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise SparkXBackendError("SPARKX_RESULT_NOT_PROVEN") from None
    return views[0]


async def _call(tool, query, token, *, authorize=None):
    if authorize is not None:
        authorize()  # Recheck the live binding before each vendor request, not just at slot start.
    try:
        return _envelope(await call_sparkx(tool, query, token=token, timeout_seconds=12), tool)
    except SparkXBackendError:
        raise
    except SparkXDirectError:
        raise SparkXBackendError("SPARKX_PROVIDER_UNAVAILABLE") from None


async def _authorized(scope, token, *, authorize=None):
    envelope = await _call("get_user_authorized_context", {}, token, authorize=authorize)
    profiles = envelope.get("profiles")
    if not isinstance(profiles, list):
        raise SparkXBackendError("SPARKX_PROFILE_NOT_PROVEN")
    matches = [row for row in profiles if isinstance(row, dict)
               and _numeric_id(row.get("profileId")) == scope["profile_id"]]
    if len(matches) != 1:
        raise SparkXBackendError("SPARKX_PROFILE_NOT_PROVEN")
    row, = matches
    store_type = row.get("storeType")
    if (not isinstance(row.get("countryCode"), str)
            or _market(row["countryCode"]) != scope["country"]
            or "storeType" in row and (not isinstance(store_type, str) or store_type not in {"seller", "vendor"})
            or any(key in row and row[key] != scope["timezone"] for key in ("timezone", "timeZone"))
            or any(key in row and row[key] != scope["currency"] for key in ("currency", "currencyCode"))):
        raise SparkXBackendError("SPARKX_AUTHORIZED_SCOPE_MISMATCH")
    # The official auth-context example omits this field. Scope may still be
    # proven, but the SP conversion lookback remains unknown for that profile.
    return store_type if "storeType" in row else "unknown"


def _field(row, *names, optional=False):
    found = [row[name] for name in names if name in row]
    if len(found) == 1:
        return found[0]
    if not found and optional:
        return None
    raise SparkXBackendError("SPARKX_ROW_FIELDS_NOT_PROVEN")


def _metric_value(value, *, count):
    if type(value) not in (int, float) and (type(value) is not str or not re.fullmatch(
            r"[0-9]{1,24}(?:\.[0-9]{1,12})?", value)):
        raise SparkXBackendError("SPARKX_METRIC_NOT_PROVEN")
    number = Decimal(str(value))
    if (not number.is_finite() or number < 0 or number >= 10**24
            or number.as_tuple().exponent < -12 or count and number != number.to_integral_value()):
        raise SparkXBackendError("SPARKX_METRIC_NOT_PROVEN")
    return int(number) if count else format(number, "f")


def _normalize(row, scope, on, granularity, now, store_type):
    if not isinstance(row, dict):
        raise SparkXBackendError("SPARKX_ROW_FIELDS_NOT_PROVEN")
    # Both the documented sample (entity-prefixed) and current plain wire keys
    # are allowed, never both for one field. Unknown dimensions/summaries fail.
    dimensions = {
        "campaignId": ("campaignId", "campaignId_", "campaign.campaignId_"),
        "campaignType": ("campaignType", "campaignType_", "campaign.campaignType_"),
        "campaignName": ("campaignName", "campaignName_", "campaign.campaignName_"),
        "profileId": ("profileId", "profileId_", "profile.profileId_"),
        "currency": ("currency", "currencyCode"),
    }
    metrics = DAILY_METRICS if granularity == "daily" else HOURLY_METRICS
    if set(row) - ({name for names in dimensions.values() for name in names}
                   | {"date"} | ({"hour"} if granularity == "hourly" else set()) | set(metrics)):
        raise SparkXBackendError("SPARKX_ROW_FIELDS_NOT_PROVEN")
    raw_profile = _field(row, *dimensions["profileId"])
    code = _field(row, *dimensions["currency"], optional=True)
    raw_day = row.get("date")
    if (_numeric_id(raw_profile) != scope["profile_id"] or code not in (None, scope["currency"])
            or not isinstance(raw_day, str) or raw_day not in (on.isoformat(), on.strftime("%Y%m%d"))):
        raise SparkXBackendError("SPARKX_ROW_SCOPE_MISMATCH")
    campaign = _numeric_id(_field(row, *dimensions["campaignId"]))
    kind = _field(row, *dimensions["campaignType"])
    name = _field(row, *dimensions["campaignName"])
    if (not campaign or kind != _SP
            or not isinstance(name, str) or not name.strip() or len(name) > 512
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise SparkXBackendError("SPARKX_CAMPAIGN_IDENTITY_NOT_PROVEN")
    clock = now.astimezone(ZoneInfo(scope["timezone"]))
    hour = row.get("hour")
    if granularity == "hourly" and (type(hour) is not int or not 0 <= hour <= 23
                                    or on == clock.date() and hour > clock.hour):
        raise SparkXBackendError("SPARKX_HOUR_NOT_PROVEN")
    values = {key: _metric_value(row.get(key), count=key not in {"Spend", "Sales"}) for key in metrics}
    if values["Clicks"] > values["Impressions"]:
        raise SparkXBackendError("SPARKX_TRAFFIC_CONTRADICTORY")
    lag = {"seller": 7, "vendor": 14}.get(store_type)
    if granularity == "hourly":
        conversion_basis = "not_requested"
    elif lag is None:
        conversion_basis = "unverified_attribution_window"
    else:
        conversion_basis = "window_elapsed" if clock.date() > on + timedelta(days=lag) else "provisional"
    return {"report_date": on.isoformat(), "hour": hour, "campaign_id": str(campaign),
            "campaign_type": "sp", "campaign_name": name, "conversion_basis": conversion_basis,
            **({"attribution_days": lag} if granularity == "daily" and lag is not None else {}), "metrics": values}


def _hint_is_warning(hint):
    """Do not archive or display provider prose to stand in for a scope proof."""
    if hint is None:
        return False
    value = " ".join(hint.replace("’", "'").casefold().split())
    for advice in _GENERIC_HINT_PARTS:
        value = value.replace(advice, "")
    if _PARTIAL_HINT.search(value):
        raise SparkXBackendError("SPARKX_SOURCE_PARTIAL_WARNING")
    return bool(value.strip(" .;"))


async def _observed(scope, token, store_type, on, granularity, now, *, authorize=None):
    clock = now.astimezone(ZoneInfo(scope["timezone"]))
    if (granularity not in SOURCES or type(on) is not date or on > clock.date()
            or granularity == "daily" and on == clock.date()):
        raise SparkXBackendError("SPARKX_REPORT_DATE_NOT_ALLOWED")
    order = ["date", "campaign.campaignId_", "campaign.campaignType_"]
    fields = [*order, "campaign.campaignName_", "profile.profileId_"]
    if granularity == "hourly":
        fields.append("hour")
        order.append("hour")
    seen, accumulated, total_hint, provider_warning = set(), [], None, False
    ordered = {"numeric", "text"}
    previous_key = None
    for page in range(1, _MAX_PAGES + 1):
        query = {"profileIds": [scope["profile_id"]], "factEntity": "campaign",
                 "dateStart": on.isoformat(), "dateEnd": on.isoformat(), "select": fields,
                 "metrics": list(DAILY_METRICS if granularity == "daily" else HOURLY_METRICS),
                 "filters": {"campaign.campaignType_": _SP},
                 "orderBy": [{"field": field, "direction": "ASC"} for field in order],
                 "timeGranularity": granularity, "userContext": "JX read-only store campaign observation",
                 "language": "en", "page": page, "pageSize": _PAGE_SIZE}
        validate_performance_query(query, frozenset({scope["profile_id"]}))
        result = await _call("get_ads_perf", query, token, authorize=authorize)
        data, meta = result.get("data"), result.get("meta")
        if (result.keys() - {"isError", "toolName", "data", "meta", "requestId"} or not isinstance(data, dict)
                or set(data) != {"rows", "rowCount"} or not isinstance(data["rows"], list)
                or not isinstance(meta, dict)
                or meta.keys() - {"page", "pageSize", "hasNextPage", "effectiveProfileIds", "currency", "hint", "total"}):
            raise SparkXBackendError("SPARKX_PAGE_NOT_PROVEN")
        rows, n, more, profiles = data["rows"], data["rowCount"], meta.get("hasNextPage"), meta.get("effectiveProfileIds")
        if (type(n) is not int or n != len(rows) or n > _PAGE_SIZE or type(more) is not bool
                or type(meta.get("page")) is not int or meta["page"] != page
                or type(meta.get("pageSize")) is not int or meta["pageSize"] != _PAGE_SIZE
                or more and n != _PAGE_SIZE
                or not isinstance(profiles, list) or len(profiles) != 1
                or _numeric_id(profiles[0]) != scope["profile_id"] or meta.get("currency") != scope["currency"]
                or "hint" in meta and not isinstance(meta["hint"], str)):
            raise SparkXBackendError("SPARKX_PAGE_SCOPE_MISMATCH")
        provider_warning = _hint_is_warning(meta.get("hint")) or provider_warning
        if len(rows) == 0:
            raise SparkXBackendError("SPARKX_EMPTY_NOT_ZERO" if page == 1 else "SPARKX_INCOMPLETE_PAGINATION")
        total = meta.get("total")
        if (("total" in meta and (type(total) is not int or total < n
                or n != min(_PAGE_SIZE, total - (page - 1) * _PAGE_SIZE)
                or more != (total > page * _PAGE_SIZE)))
                or page > 1 and total != total_hint):
            raise SparkXBackendError("SPARKX_INCOMPLETE_PAGINATION")
        total_hint = total
        for raw in rows:
            row = _normalize(raw, scope, on, granularity, now, store_type)
            key = (row["campaign_type"], row["campaign_id"], row["hour"])
            if key in seen:
                raise SparkXBackendError("SPARKX_DUPLICATE_CAMPAIGN_GRAIN")
            seen.add(key)
            # Campaign IDs can be returned as strings or numbers. Either
            # ascending ordering is valid, but one ordering must remain
            # consistent across pages. A disjoint overlap/reorder is not
            # made safe merely because it has no duplicate campaign keys.
            cid, hour = row["campaign_id"], row["hour"]
            sort_key = {"numeric": (int(cid), hour), "text": (cid, hour)}
            if previous_key is not None:
                ordered = {style for style in ordered if previous_key[style] < sort_key[style]}
                if not ordered:
                    raise SparkXBackendError("SPARKX_UNSTABLE_PAGE_ORDER")
            previous_key = sort_key
            accumulated.append(row)
        if not more:
            return {"schema": SCHEMA, "provider": "sparkx", "granularity": granularity,
                    "report_date": on.isoformat(), **scope, "store_type": store_type,
                    "page_count": page, "row_count": len(accumulated), "rows": accumulated,
                    "query_started_at_utc": now.isoformat(), "provider_watermark": None,
                    "provider_warning": provider_warning, "coverage": "observed_not_certified",
                    "usage": "observation_only_no_notifications"}
    raise SparkXBackendError("SPARKX_PAGINATION_LIMIT")


async def verify_sparkx_binding(settings, store, binding, *, query=None, now=None):
    """Probe the exact binding without archiving, mutation or sending any Feishu notification."""
    if query is not None:
        raise SparkXBackendError("SPARKX_FIXED_QUERY_REQUIRED")
    ts = _clock(now)
    scope, token = _scope(settings, store, binding)
    on = ts.astimezone(ZoneInfo(scope["timezone"])).date() - timedelta(days=1)
    try:
        with anyio.fail_after(26):
            kind = await _authorized(scope, token)
            sample = await _observed(scope, token, kind, on, "daily", ts)
    except TimeoutError:
        raise SparkXBackendError("SPARKX_PROVIDER_TIMEOUT") from None
    return {"status": "VERIFIED", **scope, "sample_date": on.isoformat(), "row_count": sample["row_count"],
            "provider_warning": sample["provider_warning"], "coverage": sample["coverage"]}


async def read_sparkx_observations(settings, store, binding, *, now=None, authorize=None):
    """Return separate partitions and safe failure codes; never present an empty partition as zero."""
    ts = _clock(now)
    scope, token = _scope(settings, store, binding)
    local = ts.astimezone(ZoneInfo(scope["timezone"])).date()
    seen, failures = {}, {}
    try:
        with anyio.fail_after(35):
            kind = await _authorized(scope, token, authorize=authorize)
            for gran, day in (("daily", local - timedelta(days=1)), ("hourly", local)):
                try:
                    seen[gran] = await _observed(scope, token, kind, day, gran, ts, authorize=authorize)
                except SparkXBackendError as exc:
                    if exc.code == "SCOPE_CHANGED":
                        raise
                    failures[gran] = exc.code
    except TimeoutError:
        failures.update({gran: "SPARKX_PROVIDER_TIMEOUT" for gran in SOURCES if gran not in seen})
    return seen, failures


def _observation_scope_id(store, selection):
    binding = selection.get("binding") or {}
    proof = selection.get("verification") or {}
    approved = [SCHEMA, str(store.sid), binding.get("profile_id"), binding.get("marketplace"),
                binding.get("currency"), binding.get("timezone"), proof.get("verified_at"),
                selection.get("selected_at")]
    return hashlib.sha256(json.dumps(approved, separators=(",", ":")).encode()).hexdigest()


def persist_sparkx_observation(db, store, selection, value, *, observed_at, slot):
    """Append in existing raw evidence storage; no Lingxing tables or delivery side effects."""
    gran = value["granularity"]
    marker = _observation_scope_id(store, selection)
    existing = db.scalar(select(RawSnapshot).where(
        RawSnapshot.source == SOURCES[gran], RawSnapshot.store_id == str(store.sid),
        RawSnapshot.payload["selection_id"].as_string() == marker,
        RawSnapshot.payload["monitor_slot"].as_string() == slot,
        RawSnapshot.payload["report_date"].as_string() == value["report_date"],
    ).order_by(RawSnapshot.id.desc()).limit(1))
    if existing:
        return {"snapshot_id": existing.id, "row_count": existing.payload["row_count"],
                "report_date": existing.payload["report_date"], "state": "OBSERVED"}
    row = RawSnapshot(source=SOURCES[gran], store_id=str(store.sid),
            captured_at=_clock(observed_at).replace(tzinfo=None),
            payload={**value, "selection_id": marker, "monitor_slot": slot})
    db.add(row)
    db.flush()
    return {"snapshot_id": row.id, "row_count": value["row_count"], "report_date": value["report_date"],
            "state": "OBSERVED"}


def list_sparkx_snapshots(db, store, selection, *, limit=2):
    """Small owner/status surface, returning no credentials, response bodies or campaign rows."""
    if selection.get("source") != "sparkx" or not selection.get("verification"):
        return []
    marker = _observation_scope_id(store, selection)
    rows = [row for source in SOURCES.values() if (row := db.scalar(select(RawSnapshot).where(
        RawSnapshot.source == source, RawSnapshot.store_id == str(store.sid),
        RawSnapshot.payload["selection_id"].as_string() == marker,
    ).order_by(RawSnapshot.captured_at.desc(), RawSnapshot.id.desc()).limit(1))) is not None]
    rows.sort(key=lambda row: (row.captured_at, row.id), reverse=True)
    rows = rows[:min(max(1, limit), len(SOURCES))]
    return [{"period": row.payload["granularity"], "report_date": row.payload["report_date"],
             "row_count": row.payload["row_count"], "captured_at": row.captured_at.isoformat() + "Z",
             "state": "OBSERVED", "coverage": "observed_not_certified",
             "provider_warning": row.payload.get("provider_warning", False),
             "usage": "observation_only_no_notifications"} for row in rows]
