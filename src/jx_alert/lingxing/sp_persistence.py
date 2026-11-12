"""Persist validated SP evidence as immutable, independently selectable versions.

The caller supplies a transaction and commits it. This module does not create
tables, initialize production connections, replace DailyMetric, or modify ads.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jx_alert.lingxing.sp_validation import (
    DAILY_PATH, HOUR_ENDPOINTS, METRIC_FIELDS, _compare, _identifier,
    _numeric, _page, _redact, _report_rows,
)
from jx_alert.storage.models import RawSnapshot
from jx_alert.storage.sp import (
    CollectionLog, ReconciliationLog, SPAdvertiseDetail, SPDailyReport,
    SPHourlyReport, SPTargetDetail,
)


SUCCESS_STATUSES = frozenset({"NO_RECORDS", "UNMATCHED", "ZERO_SPEND", "MATCHED"})
FAILURE_STATUSES = frozenset({"REQUEST_FAILED", "INVALID_RESPONSE"})
SCOPE_MODELS = {"daily": SPDailyReport, "campaign": SPHourlyReport,
                "ad_group": SPHourlyReport, "ad": SPAdvertiseDetail, "target": SPTargetDetail}


class SPPersistenceError(ValueError):
    """A fixed, non-sensitive persistence contract failure."""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _utc(value):
    if not isinstance(value, datetime):
        raise SPPersistenceError("MISSING_CAPTURE_TIME")
    return (value.astimezone(timezone.utc).replace(tzinfo=None)
            if value.tzinfo is not None else value)


def _text(value, length):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > length:
        raise SPPersistenceError("INVALID_TEXT_FIELD")
    return value


def _id(value):
    identifier = _identifier(value)
    if len(identifier) > 40:
        raise SPPersistenceError("IDENTIFIER_TOO_LONG")
    return identifier


def _optional_id(value):
    return None if value is None else _id(value)


def _witnesses(db, run_id, sid):
    db.flush()
    snapshots = db.scalars(select(RawSnapshot).where(
        RawSnapshot.store_id == sid,
        RawSnapshot.payload["run_id"].as_string() == run_id,
    ).order_by(RawSnapshot.id)).all()
    if not snapshots:
        raise SPPersistenceError("MISSING_RAW_EVIDENCE")
    return snapshots


def _partition_pages(snapshots, path, report_date, sid, campaign):
    pages = []
    for snapshot in snapshots:
        payload = snapshot.payload
        if (snapshot.source != "lingxing_sp_validation_page"
                or payload.get("endpoint") != path
                or payload.get("status") != "RECEIVED"):
            continue
        request = payload.get("request")
        if not isinstance(request, dict) or request.get("report_date") != report_date:
            continue
        if path == DAILY_PATH:
            if _id(request.get("sid")) != sid:
                raise SPPersistenceError("RAW_STORE_SCOPE_MISMATCH")
        elif _id(request.get("campaign_id")) != campaign:
            raise SPPersistenceError("RAW_CAMPAIGN_SCOPE_MISMATCH")
        pages.append(snapshot)
    if not pages:
        raise SPPersistenceError("MISSING_PARTITION_EVIDENCE")
    return pages


def _rows_from_pages(pages, *, hourly):
    result, expected = [], None
    for snapshot in pages:
        response = snapshot.payload.get("response")
        rows, total = _page(response)
        if expected is not None and total != expected:
            raise SPPersistenceError("RAW_TOTAL_CHANGED")
        expected = total
        result.extend((row, snapshot) for row in rows)
    if len(result) != expected or (hourly and len(pages) != 1):
        raise SPPersistenceError("RAW_PARTITION_INCOMPLETE")
    return result


def _assert_summary(actual, expected):
    for key in ("status", "row_count", "matched_row_count", "metrics", "profile_id"):
        if actual.get(key) != expected.get(key):
            raise SPPersistenceError("SUMMARY_RAW_MISMATCH")


def _normalized(row, snapshot, run_id, sid, scope, campaigns):
    campaign, profile = _id(row["campaign_id"]), _id(row["profile_id"])
    day = date.fromisoformat(row["report_date"])
    identity = [scope, profile, campaign, day.isoformat()]
    values = {field: _numeric(row.get(field), field) for field in METRIC_FIELDS}
    common = {
        "run_id": run_id, "store_id": sid, "profile_id": profile,
        "campaign_id": campaign, "report_date": day,
        "captured_at": _utc(snapshot.captured_at), "source_snapshot_id": snapshot.id,
        "request_id": _text(snapshot.payload["response"].get("request_id"), 200),
        "cost": values["cost"], "clicks": int(values["clicks"]),
        "impressions": int(values["impressions"]), "orders": int(values["orders"]),
        "sales": values["sales"], "raw_json": _redact(deepcopy(row), []),
    }
    if scope == "daily":
        common["campaign_name"] = _text(campaigns.get(campaign, {}).get("campaign_name"), 500)
    else:
        common["hour"] = row["hour"]
        common["group_id"] = _optional_id(row.get("group_id"))
        identity.append(row["hour"])
        if scope in {"campaign", "ad_group"}:
            common["scope"] = scope
            if scope == "ad_group":
                identity.append(_id(row["group_id"]))
        elif scope == "ad":
            common["ad_id"] = _id(row["ad_id"])
            common["asin"] = _text(row.get("asin"), 32)
            common["seller_sku"] = _text(row.get("msku"), 200)
            identity.append(common["ad_id"])
        else:
            common["targeting_id"] = _id(row["targeting_id"])
            common["ad_id"] = _optional_id(row.get("ad_id"))
            common["asin"] = _text(row.get("asin"), 32)
            common["seller_sku"] = _text(row.get("msku"), 200)
            common["match_type"] = _text(row.get("match_type"), 80)
            common["targeting"] = _text(row.get("targeting"), 65535)
            identity.append(common["targeting_id"])
    common["row_key"] = _sha(identity)
    return SCOPE_MODELS[scope](**common)


def persist_sp_validation(db, result):
    """Append an immutable run and all complete partitions, without committing.

    Replaying the exact run is idempotent. A changed payload with the same run ID
    is rejected; a new run ID creates a new version. Failed partitions get logs
    but no fact rows. A savepoint keeps a malformed run from leaving half facts.
    """
    try:
        run_id = str(UUID(result["run_id"]))
        sid, campaign = _id(result["sid"]), _id(result["campaign_id"])
        if result["status"] not in {"COMPLETE", "PARTIAL"} or not result["days"]:
            raise SPPersistenceError("INVALID_RUN_STATUS")
        safe = _redact(deepcopy(result), [])
        fingerprint = _sha(safe)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SPPersistenceError):
            raise
        raise SPPersistenceError("INVALID_RUN_CONTRACT") from None
    existing = db.get(CollectionLog, run_id)
    if existing is not None:
        if existing.content_sha256 != fingerprint:
            raise SPPersistenceError("IMMUTABLE_RUN_CONFLICT")
        return {"run_id": run_id, "status": existing.status,
                "row_counts": existing.row_counts, "reused": True}

    snapshots = _witnesses(db, run_id, sid)
    times = [_utc(row.captured_at) for row in snapshots]
    campaigns = safe.get("master", {}).get("campaigns", {})
    fact_rows, partition_logs, seen_dates = [], [], set()
    counts = {scope: 0 for scope in SCOPE_MODELS}
    for day in safe["days"]:
        report_date = day["report_date"]
        parsed_date = date.fromisoformat(report_date)
        if parsed_date.isoformat() != report_date or parsed_date in seen_dates:
            raise SPPersistenceError("INVALID_OR_DUPLICATE_REPORT_DATE")
        seen_dates.add(parsed_date)
        daily = day["daily"]
        for scope in SCOPE_MODELS:
            status = daily if scope == "daily" else day["hours"][scope]
            if status.get("status") not in SUCCESS_STATUSES | FAILURE_STATUSES:
                raise SPPersistenceError("INVALID_PARTITION_STATUS")
            captured = max(times)
            if status["status"] in SUCCESS_STATUSES:
                path = DAILY_PATH if scope == "daily" else HOUR_ENDPOINTS[scope][0]
                pages = _partition_pages(snapshots, path, report_date, sid, campaign)
                raw = _rows_from_pages(pages, hourly=scope != "daily")
                actual = _report_rows(
                    [row for row, _ in raw], report_date, campaign, campaigns,
                    hourly=scope != "daily", dimension=scope,
                )
                _assert_summary(actual, status)
                captured = max(_utc(row.captured_at) for row in pages)
                fact_rows.extend(_normalized(row, snapshot, run_id, sid, scope, campaigns)
                                 for row, snapshot in raw)
                counts[scope] += len(raw)
            comparison = ({"status": "NOT_APPLICABLE", "delta_hour_minus_daily": None}
                          if scope == "daily" else _compare(daily, status))
            if scope != "daily" and comparison != day["reconciliation"][scope]:
                raise SPPersistenceError("RECONCILIATION_RAW_MISMATCH")
            partition_logs.append(ReconciliationLog(
                run_id=run_id, store_id=sid, campaign_id=campaign,
                report_date=parsed_date, scope=scope, captured_at=captured,
                collection_status=status["status"], comparison_status=comparison["status"],
                row_count=status.get("row_count"), matched_row_count=status.get("matched_row_count"),
                daily_metrics=deepcopy(daily.get("metrics")),
                hourly_metrics=None if scope == "daily" else deepcopy(status.get("metrics")),
                delta_hour_minus_daily=deepcopy(comparison["delta_hour_minus_daily"]),
                error_code=_text(status.get("error_code"), 100),
            ))
    log = CollectionLog(run_id=run_id, store_id=sid, campaign_id=campaign,
                        status=safe["status"], started_at=min(times), finished_at=max(times),
                        content_sha256=fingerprint, row_counts=counts, summary=safe)
    connection = db.connection()
    if (connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction):
        # Python's SQLite legacy transaction mode does not BEGIN for a SELECT.
        # An outer SAVEPOINT would otherwise commit on RELEASE even though the
        # caller has not committed its Session. Start the outer transaction.
        connection.exec_driver_sql("BEGIN")
    try:
        with db.begin_nested():
            db.add(log)
            db.flush()
            db.add_all(fact_rows)
            db.add_all(partition_logs)
            db.flush()
    except IntegrityError:
        # Concurrent replays may race on the run PK. The unique constraints
        # prevent accumulation; only an identical committed run is reusable.
        existing = db.get(CollectionLog, run_id)
        if existing is None or existing.content_sha256 != fingerprint:
            raise SPPersistenceError("RUN_WRITE_CONFLICT") from None
        return {"run_id": run_id, "status": existing.status,
                "row_counts": existing.row_counts, "reused": True}
    return {"run_id": run_id, "status": safe["status"], "row_counts": counts, "reused": False}


def latest_sp_partition(db, *, sid, campaign_id, report_date, scope="daily"):
    """Select one complete version, explicitly flagging a later failed attempt.

    Fact rows are ORM instances. Metrics are Decimal totals for this one scope;
    NO_RECORDS/UNMATCHED keep metrics=None. Empty success supersedes old rows.
    The selected campaign must be the target of that collection run.
    """
    if scope not in SCOPE_MODELS:
        raise ValueError("Unknown SP scope")
    sid, campaign = _id(sid), _id(campaign_id)
    day = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    query = select(ReconciliationLog).where(
        ReconciliationLog.store_id == sid, ReconciliationLog.campaign_id == campaign,
        ReconciliationLog.report_date == day, ReconciliationLog.scope == scope,
    ).order_by(ReconciliationLog.captured_at.desc(), ReconciliationLog.id.desc())
    latest_attempt = db.scalars(query.limit(1)).first()
    selected = db.scalars(query.where(
        ReconciliationLog.collection_status.in_(SUCCESS_STATUSES),
    ).limit(1)).first()
    if selected is None:
        return {"run_id": None, "status": "UNAVAILABLE", "comparison_status": "UNAVAILABLE",
                "rows": [], "metrics": None, "stale_after_failure": False,
                "latest_attempt_status": latest_attempt.collection_status if latest_attempt else None}
    model = SCOPE_MODELS[scope]
    row_query = select(model).where(
        model.run_id == selected.run_id, model.store_id == sid,
        model.campaign_id == campaign, model.report_date == day,
    )
    if scope in {"campaign", "ad_group"}:
        row_query = row_query.where(model.scope == scope)
    rows = db.scalars(row_query.order_by(model.row_key)).all()
    metrics = ({field: sum((Decimal(getattr(row, field)) for row in rows), Decimal(0))
                for field in METRIC_FIELDS} if rows else None)
    return {"run_id": selected.run_id, "status": selected.collection_status,
            "comparison_status": selected.comparison_status,
            "rows": rows, "metrics": metrics,
            "stale_after_failure": latest_attempt.run_id != selected.run_id,
            "latest_attempt_status": latest_attempt.collection_status}
