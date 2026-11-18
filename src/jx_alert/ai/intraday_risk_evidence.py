"""Verified SP intraday budget risk, separate from mature conversion evidence.

The signal means current observed spend exceeds a freshly observed daily budget
and the source itself reports a same-day over-budget event. It is an operational
review threshold, not a claim of an Amazon violation or a hard spending cap.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.lingxing.intraday_observation import SOURCE
from jx_alert.storage.models import Campaign, RawSnapshot


def _utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _number(value, *, count=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0 or count and not result.is_integer():
        return None
    return result


def _fresh(value, clock, maximum):
    stamp = _utc(value)
    return stamp is not None and clock - maximum <= stamp <= clock


def _event_date(value, on, zone, clock):
    """Convert only explicit offsets; keep naive source dates literal."""
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is not None and observed.utcoffset() is not None:
        return "explicit_offset" if observed <= clock and observed.astimezone(zone).date() == on else None
    # This is a source-date assertion, not an invented UTC/Pacific conversion.
    return "raw_date" if value[:10] == on.isoformat() else None


def _budget(record):
    raw = record.get("budget")
    kind = record.get("budget_type")
    if raw is None and record.get("daily_budget") is not None:
        raw, kind = record["daily_budget"], kind or "daily"
    return _number(raw), kind.lower() if isinstance(kind, str) else None


def intraday_over_budget_candidates(db, store, snapshot, *, now=None, max_age_minutes=75):
    """Return only supported current-SP risk candidates, without writes or notices.

    Snapshot capture time, current master and cached master fields all need to
    agree. Missing, failed, fallback or stale observations are silently excluded.
    Intraday attribution is intentionally absent from the returned evidence.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Intraday risk needs a timezone-aware clock")
    if (isinstance(max_age_minutes, bool) or not isinstance(max_age_minutes, (int, float))
            or not math.isfinite(max_age_minutes) or max_age_minutes <= 0):
        raise ValueError("Intraday freshness must be a positive number of minutes")
    clock = clock.astimezone(timezone.utc)
    maximum = timedelta(minutes=max_age_minutes)
    try:
        zone = ZoneInfo(store.timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return []
    sid, on = str(store.sid), clock.astimezone(zone).date()
    if (store.status != 1 or store.has_ads_setting != 1 or snapshot is None
            or snapshot.source != SOURCE or str(snapshot.store_id) != sid
            or not _fresh(snapshot.captured_at, clock, maximum)):
        return []
    payload = snapshot.payload
    if (not isinstance(payload, dict) or payload.get("availability") != "AVAILABLE"
            or payload.get("observation_only") is not True or payload.get("observation_date") != on.isoformat()
            or payload.get("report_timezone", store.timezone) != store.timezone
            or not isinstance(payload.get("rows"), list)):
        return []
    # The budget/current-spend equivalence is verified for OpenAPI observations.
    # MCP campaign UI observations remain useful to chat, but cannot silently be
    # promoted into this source-specific live budget alarm.
    if payload.get("sources") not in (None, [], ["lingxing_openapi"]):
        return []
    latest = db.scalar(select(RawSnapshot.id).where(RawSnapshot.store_id == sid,
        RawSnapshot.source == SOURCE, RawSnapshot.payload["observation_date"].as_string() == on.isoformat())
        .order_by(RawSnapshot.id.desc()).limit(1))
    if snapshot.id is None or latest != snapshot.id:
        return []
    master = db.scalar(select(RawSnapshot).where(RawSnapshot.store_id == sid,
        RawSnapshot.source == "lingxing_sp_campaigns").order_by(RawSnapshot.id.desc()).limit(1))
    if (master is None or not _fresh(master.captured_at, clock, maximum)
            or _utc(master.captured_at) < _utc(snapshot.captured_at) - timedelta(minutes=15)):
        return []
    master_payload = master.payload
    if (not isinstance(master_payload, dict) or master_payload.get("status") not in (None, "COMPLETE", "SUCCEEDED")
            or not isinstance(master_payload.get("rows"), list)):
        return []
    masters, observations = {}, {}
    for row in master_payload["rows"]:
        if isinstance(row, dict) and row.get("campaign_id") is not None:
            masters.setdefault(str(row["campaign_id"]), []).append(row)
    for row in payload["rows"]:
        if isinstance(row, dict) and row.get("campaign_id") is not None:
            observations.setdefault(str(row["campaign_id"]), []).append(row)
    campaigns = db.scalars(select(Campaign).where(Campaign.store_id == sid, Campaign.campaign_type == "sp",
        Campaign.campaign_id.in_(observations))).all()
    results = []
    for campaign in campaigns:
        cid = campaign.campaign_id
        raw_master, raw_observed = masters.get(cid, []), observations[cid]
        if (len(raw_master) != 1 or len(raw_observed) != 1 or (campaign.state or "").lower() != "enabled"
                or not _fresh(campaign.updated_at, clock, maximum)
                or _utc(campaign.updated_at) < _utc(master.captured_at) - timedelta(minutes=15)):
            continue
        master_row, observed = raw_master[0], raw_observed[0]
        if ((str(master_row.get("state") or "")).lower() != "enabled"
                or str(observed.get("report_date")) != on.isoformat()
                or observed.get("sid") is not None and str(observed["sid"]) != sid
                or observed.get("_source") not in (None, "lingxing_openapi")
                or observed.get("_attribution_unconfirmed") is True
                or observed.get("confirmed_currency") != "USD"):
            continue
        budget, kind = _budget(master_row)
        if (kind != "daily" or (campaign.budget_type or "").lower() != "daily"
                or budget is None or budget <= 0 or budget != _number(campaign.budget)):
            continue
        event = master_row.get("lastOverBudgetAt") or master_row.get("last_over_budget_at")
        if event != campaign.last_over_budget_at:
            continue
        date_basis = _event_date(event, on, zone, clock)
        if date_basis is None:
            continue
        spend = _number(observed.get("cost"))
        clicks, impressions = (_number(observed.get(key), count=True) for key in ("clicks", "impressions"))
        if (spend is None or clicks is None or impressions is None
                or spend < budget * 1.2 or spend - budget < 30):
            continue
        row = {"store_id": sid, "store_name": store.name, "campaign_id": cid,
            "campaign_name": campaign.name or cid, "ad_type": "sp", "current_state": "投放中",
            "state_observed_at": _utc(campaign.updated_at).isoformat(), "report_date": on.isoformat(),
            "mature_date": on.isoformat(), "intraday": True, "currency": "USD",
            "current": {"spend": spend, "clicks": clicks, "impressions": impressions},
            "mature": {}, "baseline": {}, "kpis": {}, "verified_daily_budget": budget,
            "material_signals": ["intraday_budget"], "source": SOURCE,
            "observation_captured_at": _utc(snapshot.captured_at).isoformat(),
            "source_budget_event": event, "source_budget_event_date_basis": date_basis,
            "proof": {"snapshot_id": snapshot.id, "master_snapshot_id": master.id,
                "master_captured_at": _utc(master.captured_at).isoformat(),
                "budget": budget, "state": campaign.state, "over_budget_event": event}}
        row["evidence_id"] = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        results.append(row)
    return results
