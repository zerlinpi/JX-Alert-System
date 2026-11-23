"""Read-only SparkX collection for the daily report, 7-day review and hourly monitor.

All numbers come from get_ads_perf (and enabled-campaign counts from
get_entity_metadata). Every monitored profile is queried in one multi-profile
call per need, so SparkX converts money to one currency (USD for several
profiles). A missing row stays missing and is never turned into zero.
Store-local dates follow the owner's report timezone, America/Los_Angeles.
"""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import anyio

from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import (
    SparkXError, authorized_profiles, call, day, num, open_session, pages, pid, run, validate_token,
)

MARKETPLACE = ZoneInfo("America/Los_Angeles")
BASE_METRICS = ("Impressions", "Clicks", "Spend", "Sales", "Conversions", "Units")
FIELDS = {"Impressions": "impressions", "Clicks": "clicks", "Spend": "spend", "Sales": "sales",
          "Conversions": "orders", "Units": "units"}
METRIC_KEYS = tuple(FIELDS.values())
BUSINESS = {"TotalSalesAmount": "total_sales", "OrderCount": "total_orders", "UnitCount": "total_units"}
CAMPAIGN_SELECT = ["profile.profileId_", "campaign.campaignId_", "campaign.campaignName_",
                   "campaign.campaignType_", "campaign.campaignState_"]
METADATA_PAUSE = 0.55
CONTEXT = {"daily": "JX 只读日报：各店广告与店铺总销售（昨日对比前日）",
           "review": "JX 只读复盘：近7日对比前7日 campaign 异常",
           "hourly": "JX 只读监控：今日截至目前 campaign 花费与转化",
           "roster": "JX 只读：读取已授权店铺"}


def report_dates(now):
    """Latest completed marketplace day, the day before it and two 7-day windows."""
    latest = now.astimezone(MARKETPLACE).date() - timedelta(days=1)
    return {"report_date": latest, "previous_date": latest - timedelta(days=1),
            "window": (latest - timedelta(days=6), latest),
            "previous_window": (latest - timedelta(days=13), latest - timedelta(days=7))}


def marketplace_today(now):
    return now.astimezone(MARKETPLACE).date()


def metrics(row):
    values = {target: num(row.get(source)) if source in row else None for source, target in FIELDS.items()}
    if None not in (values["clicks"], values["impressions"]) and values["clicks"] > values["impressions"]:
        raise SparkXError("SPARKX_TRAFFIC_CONTRADICTORY")
    return values


def _ids(values):
    return [int(value) for value in values]


async def _daily(session, ids, dates, out):
    start, end = dates["previous_date"].isoformat(), dates["report_date"].isoformat()
    rows, currency = await pages(session, "get_ads_perf", {
        "profileIds": _ids(ids), "factEntity": "campaign", "dateStart": start, "dateEnd": end,
        "metrics": list(BASE_METRICS), "select": ["date", "profile.profileId_"],
        "orderBy": [{"field": "profile.profileId_", "direction": "ASC"}],
        "userContext": CONTEXT["daily"], "language": "en"}, ids)
    out["currency"] = currency
    daily = {}
    for row in rows:
        key, when = pid(row.get("profileId")), day(row.get("date"))
        if key not in ids or when not in (start, end):
            raise SparkXError("SPARKX_ROW_SCOPE_MISMATCH")
        if when in daily.setdefault(key, {}):
            raise SparkXError("SPARKX_DUPLICATE_ROW")
        daily[key][when] = metrics(row)
    out["daily"] = daily
    try:
        rows, business_currency = await pages(session, "get_ads_perf", {
            "profileIds": _ids(ids), "factEntity": "asin", "dateStart": start, "dateEnd": end,
            "metrics": list(BUSINESS), "select": ["date", "profile.profileId_"],
            "userContext": CONTEXT["daily"], "language": "en"}, ids)
        if business_currency != currency:
            raise SparkXError("SPARKX_CURRENCY_UNCONFIRMED")
        business = {}
        for row in rows:
            key, when = pid(row.get("profileId")), day(row.get("date"))
            if key not in ids or when not in (start, end) or when in business.setdefault(key, {}):
                raise SparkXError("SPARKX_ROW_SCOPE_MISMATCH")
            business[key][when] = {target: num(row.get(source)) if source in row else None
                                   for source, target in BUSINESS.items()}
        out["business"] = business
    except SparkXError as exc:
        out["business"] = None
        out["errors"]["business"] = exc.code  # Total sales/TACOS stay unknown; the report continues.


def _campaign_entry(entries, key, cid, row):
    entry = entries.setdefault(key, {}).setdefault(cid, {"name": None, "type": None, "state": None})
    name = row.get("campaignName")
    if not entry["name"]:
        entry["name"] = name.strip()[:200] if isinstance(name, str) and name.strip() else cid
    if not entry["type"] and isinstance(row.get("campaignType"), str):
        entry["type"] = row["campaignType"][:40]
    if isinstance(row.get("campaignState"), str):
        entry["state"] = row["campaignState"].strip().lower()[:20]
    return entry


async def _campaign_rows(session, ids, start, end, context):
    return await pages(session, "get_ads_perf", {
        "profileIds": _ids(ids), "factEntity": "campaign", "dateStart": start.isoformat(),
        "dateEnd": end.isoformat(), "metrics": list(BASE_METRICS), "select": CAMPAIGN_SELECT,
        "orderBy": [{"field": "profile.profileId_", "direction": "ASC"},
                    {"field": "campaign.campaignId_", "direction": "ASC"}],
        "userContext": context, "language": "en"}, ids)


async def _review(session, ids, dates, out):
    campaigns = {}
    for slot, (start, end) in (("w", dates["window"]), ("p", dates["previous_window"])):
        rows, currency = await _campaign_rows(session, ids, start, end, CONTEXT["review"])
        if out["currency"] is not None and currency != out["currency"]:
            raise SparkXError("SPARKX_CURRENCY_UNCONFIRMED")
        out["currency"] = currency
        for row in rows:
            key, cid = pid(row.get("profileId")), pid(row.get("campaignId"))
            if key not in ids or cid is None:
                raise SparkXError("SPARKX_ROW_SCOPE_MISMATCH")
            entry = _campaign_entry(campaigns, key, cid, row)
            if slot in entry:
                raise SparkXError("SPARKX_DUPLICATE_ROW")
            entry[slot] = metrics(row)
    for items in campaigns.values():
        for entry in items.values():
            entry.setdefault("w", None)
            entry.setdefault("p", None)
    out["campaigns"] = campaigns
    idle = [key for key in ids if not any(
        ((item["w"] or {}).get("impressions") or 0) > 0 or ((item["p"] or {}).get("impressions") or 0) > 0
        for item in campaigns.get(key, {}).values())]
    out["enabled_campaigns"] = {}
    try:
        # One call per idle store (rows carry no reliable profileId), paced under 120 reads/min.
        for key in idle[:60]:
            view = await call(session, "get_entity_metadata", {
                "entity": "campaign", "profileIds": [int(key)], "filters": {"campaignState": "enabled"},
                "select": ["campaignId"], "page": 1, "pageSize": 1, "userContext": CONTEXT["review"]})
            meta, data = view.get("meta"), view.get("data")
            total = meta.get("total") if isinstance(meta, dict) else None
            if type(total) is not int or total < 0:
                rows = data.get("rows") if isinstance(data, dict) else None
                if not isinstance(rows, list) or (isinstance(meta, dict) and meta.get("hasNextPage")):
                    raise SparkXError("SPARKX_PAGE_NOT_PROVEN")
                total = len(rows)
            out["enabled_campaigns"][key] = total
            await anyio.sleep(METADATA_PAUSE)
    except SparkXError as exc:
        out["enabled_campaigns"] = None
        out["errors"]["enabled_campaigns"] = exc.code


async def _hourly(session, ids, today, out):
    rows, currency = await _campaign_rows(session, ids, today, today, CONTEXT["hourly"])
    out["currency"] = currency
    campaigns = {}
    for row in rows:
        key, cid = pid(row.get("profileId")), pid(row.get("campaignId"))
        if key not in ids or cid is None:
            raise SparkXError("SPARKX_ROW_SCOPE_MISMATCH")
        entry = _campaign_entry(campaigns, key, cid, row)
        if "t" in entry:
            raise SparkXError("SPARKX_DUPLICATE_ROW")
        entry["t"] = metrics(row)
    out["campaigns"] = campaigns


def _scope(roster, directory):
    profiles = {key: value for key, value in roster.items() if directory_module.monitored(directory, key)}
    disabled = sorted((key for key in roster if key not in profiles), key=int)
    missing = sorted((key for key, entry in directory["profiles"].items()
                      if entry["monitored"] and key not in roster), key=int)
    return profiles, disabled, missing


def collect(settings, kind, *, now, token=None, directory=None, on_roster=None, total_timeout=240.0):
    """One read-only SparkX session for one lane. Raises SparkXError with a safe code."""
    from jx_alert.sparkx.credentials import effective_token

    if kind not in {"daily", "review", "hourly"}:
        raise ValueError("Unknown SparkX collection")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("SparkX collection requires a timezone-aware clock")
    token = effective_token(settings) if token is None else token
    if not token:
        raise SparkXError("SPARKX_NOT_CONFIGURED", 409)
    if not validate_token(token):
        raise SparkXError("SPARKX_INVALID_TOKEN_FORMAT", 409)
    directory = directory_module.load(settings) if directory is None else directory
    timeout = float(getattr(settings, "sparkx_timeout", 30.0))
    dates = report_dates(now)
    out = {"kind": kind, "generated_at": now.astimezone(timezone.utc).isoformat(), "currency": None,
           "errors": {}, "roster": {}, "profiles": {}, "disabled": [], "missing": []}
    if kind == "daily":
        out.update(report_date=dates["report_date"].isoformat(), previous_date=dates["previous_date"].isoformat())
    elif kind == "review":
        out.update(window=[value.isoformat() for value in dates["window"]],
                   previous_window=[value.isoformat() for value in dates["previous_window"]])
    else:
        out.update(day=marketplace_today(now).isoformat(),
                   marketplace_time=now.astimezone(MARKETPLACE).strftime("%m-%d %H:%M"))

    async def conversation():
        async with open_session(token, timeout) as session:
            roster = await authorized_profiles(session)
            out["roster"] = roster
            profiles, out["disabled"], out["missing"] = _scope(roster, directory)
            out["profiles"] = profiles
            if not profiles:
                raise SparkXError("SPARKX_NO_PROFILES", 409)
            ids = sorted(profiles, key=int)
            if kind == "daily":
                await _daily(session, ids, dates, out)
            elif kind == "review":
                await _review(session, ids, dates, out)
            else:
                await _hourly(session, ids, marketplace_today(now), out)
            return out

    try:
        return run(conversation, total_timeout=total_timeout)
    finally:
        if on_roster is not None and out["roster"]:
            on_roster(out["roster"])


def fetch_roster(settings, *, token=None, timeout=20.0):
    """Authorized profiles only (no metrics), for the console store directory."""
    from jx_alert.sparkx.credentials import effective_token

    token = effective_token(settings) if token is None else token
    if not token:
        raise SparkXError("SPARKX_NOT_CONFIGURED", 409)

    async def conversation():
        async with open_session(token, timeout) as session:
            return await authorized_profiles(session)

    return run(conversation, total_timeout=timeout + 10)
