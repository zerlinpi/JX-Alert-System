"""Bounded, scoped refresh of warehouse facts for interactive ad questions."""
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from jx_alert.lingxing.ad_currency import account_currencies, advertising_currency
from jx_alert.lingxing.attribution import AD_ATTRIBUTION_DAYS
from jx_alert.lingxing.client import LingxingClient
from jx_alert.lingxing.collector import collect_ads_for_day
from jx_alert.lingxing.source_credentials import mcp_credentials, SourceCredentialsError
from jx_alert.lingxing.source_gateway import CollectionSources
from jx_alert.operations import CollectionConfigurationError, _checked_scope, store_report_plan
from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, RawSnapshot, Store, SyncJob
from jx_alert.store_controls import collection_states


_MAX_PARTITIONS = 8
_MAX_MCP_PARTITIONS = 2
_MAX_SECONDS = 90
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_RETRY_INTERVAL = timedelta(minutes=15)
_JOB_TYPE = "feishu_ad_evidence_refresh"
_REQUIRED = ("cost", "orders", "sales", "units", "clicks", "impressions")


def _utc(value):
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _partition_plan(db, settings, requested, kinds, clock, may_collect):
    scope = _checked_scope(settings, None)
    wanted = {sid: day for sid, day in requested.items() if sid in scope and may_collect(sid)}
    enabled = collection_states(settings, list(wanted))
    stores = list(db.scalars(select(Store).where(Store.sid.in_(wanted), Store.status == 1)))
    plan = store_report_plan([{"sid": shop.sid, "status": shop.status} for shop in stores],
                             settings.store_timezones_file, now=clock)
    prioritized = []
    for shop in plan:
        sid = shop["store_id"]
        if not enabled[sid] or not shop["timezone_confirmed"] or shop["state"] != "READY":
            continue
        target = wanted[sid]
        latest = date.fromisoformat(shop["report_date"])
        if not latest - timedelta(days=45) <= target <= latest:
            continue
        zone = ZoneInfo(shop["timezone"])
        for kind in kinds:
            lag = AD_ATTRIBUTION_DAYS[kind]
            mature = target - timedelta(days=lag)
            for rank, (day, delay) in enumerate(((target, 0), (mature, lag),
                (target - timedelta(days=1), 0), (mature - timedelta(days=1), lag))):
                boundary = datetime.combine(day + timedelta(days=delay + 1), time.min, zone).astimezone(timezone.utc)
                prioritized.append((rank, sid, kind, day, boundary))
    if not prioritized:
        return []
    days = {item[3] for item in prioritized}
    coverage = {(r.store_id, r.dimension, r.metric_date): r for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id.in_(wanted), DataCoverage.metric_date.in_(days),
        DataCoverage.dimension.in_([f"campaign_{kind}" for kind in kinds])))}
    confirmed_money = {(sid, dimension, day, field): count for sid, dimension, day, field, count in db.execute(
        select(DailyMetric.store_id, DailyMetric.dimension, DailyMetric.metric_date,
            DailyMetric.metric_name, func.count()).where(
            DailyMetric.store_id.in_(wanted), DailyMetric.metric_date.in_(days),
            DailyMetric.dimension.in_([f"campaign_{kind}" for kind in kinds]),
            DailyMetric.metric_name.in_(("spend", "sales")), DailyMetric.currency == "USD",
            DailyMetric.source == func.replace(DailyMetric.dimension, "campaign_", "lingxing_ads_"),
        ).group_by(DailyMetric.store_id, DailyMetric.dimension, DailyMetric.metric_date, DailyMetric.metric_name))}
    source_day = RawSnapshot.payload["report_date"].as_string()
    snapshots = {(sid, source, day): captured for sid, source, day, captured in db.execute(select(
        RawSnapshot.store_id, RawSnapshot.source, source_day, func.max(RawSnapshot.captured_at)).where(
        RawSnapshot.store_id.in_(wanted), source_day.in_([day.isoformat() for day in days]),
        RawSnapshot.source.in_([f"lingxing_ads_{kind}_campaign_report" for kind in kinds]),
    ).group_by(RawSnapshot.store_id, RawSnapshot.source, source_day))}
    since = (clock - _RETRY_INTERVAL).replace(tzinfo=None)
    recent = set()
    for entry in db.scalars(select(SyncJob.metadata_json).where(SyncJob.job_type == _JOB_TYPE,
            SyncJob.started_at >= since)):
        recent.update((r["sid"], r["kind"], r["day"]) for r in (entry or {}).get("reports", []))

    pending = []
    for rank, sid, kind, day, boundary in sorted(prioritized):
        key = (sid, f"campaign_{kind}", day)
        row = coverage.get(key)
        snapshot = snapshots.get((sid, f"lingxing_ads_{kind}_campaign_report", day.isoformat()))
        if (row is not None and row.status == "COMPLETE" and row.row_count > 0
                and row.completed_at is not None and boundary <= _utc(row.completed_at) <= clock
                and snapshot is not None and boundary <= _utc(snapshot) <= clock
                and all(confirmed_money.get((*key, field)) == row.row_count for field in ("spend", "sales"))):
            continue
        if (sid, kind, day.isoformat()) not in recent:
            pending.append((rank, sid, kind, day))
    return pending[:_MAX_PARTITIONS]


def _provider_store(row):
    if not isinstance(row, dict):
        return False
    try:
        return int(row.get("status") or 0) == 1 and int(row.get("has_ads_setting") or 0) == 1
    except (TypeError, ValueError):
        return False


def _validated_report(rows, sid, day):
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or str(row.get("sid", sid)) != sid or row.get("report_date") != day:
            return False
        cid = row.get("campaign_id")
        if type(cid) not in (int, str) or not str(cid).strip() or str(cid) in seen:
            return False
        seen.add(str(cid))
        try:
            if any(isinstance(row[key], bool) or not math.isfinite(float(row[key]))
                   or float(row[key]) < 0 for key in _REQUIRED):
                return False
        except (TypeError, ValueError, OverflowError, KeyError):
            return False
    return len(json.dumps(rows, ensure_ascii=True).encode("utf-8")) <= _MAX_REPORT_BYTES


class _FetchedReport:
    def __init__(self, rows):
        self.rows = rows

    def iter_advertising_report(self, _kind, _payload):
        return iter(self.rows)


class _ObservedAdAccounts:
    def __init__(self, accounts):
        self.accounts = accounts

    def seller_ad_accounts(self):
        return self.accounts


def _still_allowed(db, settings, sid, may_collect):
    store = db.get(Store, sid, populate_existing=True)
    return (store is not None and store.status == 1 and may_collect(sid)
            and collection_states(settings, [sid])[sid])


def _collect_partition(db, settings, api, record, sid, kind, day, currencies, may_collect):
    payload = {"sid": record["sid"], "report_date": day, "length": 100}
    if kind in {"sp", "sd"}:
        payload["show_detail"] = 1
    rows = list(api.iter_advertising_report(kind, payload, max_pages=12))
    if not rows:
        return "EMPTY"
    if not _validated_report(rows, sid, day):
        return "FAILED"
    profiles = dict(db.execute(select(Campaign.campaign_id, Campaign.profile_id).where(
        Campaign.store_id == sid, Campaign.campaign_type == kind)).all())
    # A failed profile lookup must not replace established monetary facts
    # with missing currency or attach a campaign to the wrong profile.
    if any(advertising_currency(currencies, sid, row,
            campaign_profile=profiles.get(str(row["campaign_id"]))) != "USD" for row in rows):
        return "FAILED"
    if not _still_allowed(db, settings, sid, may_collect):
        return "SKIPPED"
    collect_ads_for_day(_FetchedReport(rows), db, {sid: day}, stores_response={"data": [record]},
                        ad_currencies=currencies, report_types=(kind,))
    return "REFRESHED"


def refresh_ad_evidence(db, settings, stores, report_dates, *, ad_types=("sp",), now=None,
                        may_collect, client_factory=None):
    """Attempt missing report partitions; never downgrade previously usable facts.

    The caller must supply already-authorized stores and a live per-store
    operator/admin permission check. MCP fallback uses its observational grain
    and cannot populate conversions or make an API partition complete.
    """
    result = dict(planned=0, attempted=0, refreshed=0, empty=0, failed=0, busy=False, mcp_observations=0)
    if (settings.app_dry_run or not settings.scheduler_enabled or not settings.mcp_operations_enabled):
        return result
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Interactive advertising refresh requires an aware clock")
    kinds = tuple(dict.fromkeys(ad_types))
    if not kinds or set(kinds) - AD_ATTRIBUTION_DAYS.keys():
        raise ValueError("Unsupported advertising report type")
    requested = {}
    for store in stores:
        sid = str(store.sid)
        raw = report_dates.get(sid)
        if raw is not None and store.status == 1:
            on = date.fromisoformat(raw) if isinstance(raw, str) else raw
            if type(on) is date:
                requested[sid] = on
    if not requested:
        return result
    engine = db.get_bind()
    try:
        with database_job_lock(engine, "hourly_ad_monitor"), database_job_lock(engine, "daily_pipeline"):
            plan = _partition_plan(db, settings, requested, kinds, clock, may_collect)
            result["planned"] = len(plan)
            if not plan:
                return result
            db.rollback()
            deadline = monotonic() + _MAX_SECONDS
            try:
                api = (client_factory or LingxingClient.from_settings)()
                records = api.get_stores()
                supported = {str(row.get("sid")): row for row in records.get("data", []) if _provider_store(row)}
            except Exception:
                # Do not infer a current API authorization from cached inventory.
                api, supported = None, {}
            try:
                accounts = api.seller_ad_accounts(max_pages=12) if api else []
                currencies = account_currencies(accounts)
            except Exception:
                accounts, currencies = [], {}
            try:
                mcp = (CollectionSources(_ObservedAdAccounts(accounts), settings, db=db)
                       if mcp_credentials(settings) is not None else None)
            except SourceCredentialsError:
                mcp = None
            outcomes = []
            mcp_attempted = 0
            for rank, sid, kind, day in plan:
                if monotonic() >= deadline:
                    break
                if (api is not None and sid not in supported) or not _still_allowed(
                        db, settings, sid, may_collect):
                    continue
                date_text = day.isoformat()
                state = "FAILED"
                if sid in supported:
                    try:
                        state = _collect_partition(db, settings, api, supported[sid], sid, kind,
                                                   date_text, currencies, may_collect)
                    except Exception:
                        db.rollback()
                if state == "SKIPPED":
                    continue
                result["attempted"] += 1
                result[state.lower()] += 1
                outcomes.append({"sid": sid, "kind": kind, "day": date_text, "status": state})
                # A failed conversion source must never be replaced by MCP
                # order guesses. Keep only separately archived observations.
                if (rank == 0 and mcp is not None and state in {"EMPTY", "FAILED"}
                        and mcp_attempted < _MAX_MCP_PARTITIONS and monotonic() < deadline
                        and _still_allowed(db, settings, sid, may_collect)):
                    mcp_attempted += 1
                    try:
                        if mcp.observe_campaign(sid, date_text, kind):
                            result["mcp_observations"] += 1
                        if _still_allowed(db, settings, sid, may_collect):
                            mcp.flush(db, raw_only=True)
                        else:
                            mcp.pending.clear()
                    except Exception:
                        db.rollback()
                        mcp.pending.clear()
            if outcomes:
                db.add(SyncJob(job_type=_JOB_TYPE, status="SUCCEEDED" if not result["failed"] else "PARTIAL_FAILED",
                    started_at=clock.astimezone(timezone.utc).replace(tzinfo=None),
                    finished_at=datetime.now(timezone.utc).replace(tzinfo=None), metadata_json={"reports": outcomes}))
                db.commit()
    except (CollectionConfigurationError, ZoneInfoNotFoundError):
        db.rollback()
    except JobAlreadyRunning:
        result["busy"] = True
    return result
