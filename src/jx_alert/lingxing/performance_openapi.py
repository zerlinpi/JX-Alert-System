"""Read-only collection of Lingxing store performance and performance notices.

The collector deliberately keeps source freshness separate from HTTP request time.
Missing/old source timestamps never become a healthy/empty result.
"""
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.analysis.service import enqueue_anomaly_notifications, upsert_anomalies
from jx_alert.storage.models import (
    PerformanceNotice,
    PerformanceSyncState,
    RawSnapshot,
    StorePerformance,
)

LIST_PATH = "/basicOpen/customerService/storeTarget/list"
DETAIL_PATH = "/basicOpen/customerService/storeTarget/detail"
NOTICE_PATH = "/basicOpen/customerService/performanceNotice/list"

INDICATOR_DIRECTIONS = {
    "order_with_defect": "high",
    "late_shipment": "high",
    "pre_fulfillment_cancellation": "high",
    "valid_tracking": "low",
    "on_time_delivery": "low",
    "fba_order_with_defect": "high",
    "invoice_defect": "high",
}
_STATUS_LABELS = {0: "UNKNOWN", 1: "PENDING", 2: "PROCESSED", 3: "NO_ACTION"}
_RISK_STATUSES = {
    "atrisk", "risk", "critical", "unhealthy", "suspended", "deactivated",
    "有风险", "风险", "严重", "不健康", "停用", "停权",
}
_NORMAL_STATUSES = {"healthy", "normal", "good", "正常", "健康", "健康正常"}


def _utcnow():
    return datetime.now(timezone.utc)


def _sid_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Lingxing sid must be an integer-compatible value: {value!r}")


def _normalize_key(value):
    return "".join(char.lower() for char in str(value) if char.isalnum())


def _extract_rows(response, label):
    if not isinstance(response, dict):
        raise RuntimeError(f"{label} response must be a JSON object")
    data = response.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "records", "rows", "items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    raise RuntimeError(f"{label} response does not contain a recognized row array")


def _extract_total(response):
    data = response.get("data")
    candidates = [response.get("total"), response.get("count")]
    if isinstance(data, dict):
        candidates.extend([data.get("total"), data.get("count")])
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Lingxing performance pagination total is invalid") from exc
    return None


def _fetch_all(fetch_page, *, page_size, max_pages, label):
    rows = []
    pages = []
    offset = 0
    for _ in range(max_pages):
        response = fetch_page(offset, page_size)
        page_rows = _extract_rows(response, label)
        pages.append((offset, response))
        rows.extend(page_rows)
        total = _extract_total(response)
        next_offset = offset + len(page_rows)
        if total is not None:
            if next_offset > total:
                raise RuntimeError(f"{label} returned more rows than its reported total")
            if next_offset == total:
                return rows, pages
            if not page_rows:
                raise RuntimeError(
                    f"{label} pagination incomplete: {next_offset} of {total} rows returned"
                )
        elif len(page_rows) < page_size:
            return rows, pages
        if not page_rows:
            return rows, pages
        offset = next_offset
    raise RuntimeError(f"{label} exceeded max_pages={max_pages}")


def _timestamp_text(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    return text or None


def _parse_source_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def _freshness_status(source_timestamp, settings, *, now=None):
    timezone_name = str(getattr(settings, "lingxing_performance_source_timezone", "") or "").strip()
    if not timezone_name:
        return "NOT_CONFIGURED"
    try:
        source_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return "NOT_CONFIGURED"
    parsed = _parse_source_datetime(source_timestamp)
    if parsed is None:
        return "MISSING"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_zone)
    now = now or _utcnow()
    age = now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)
    if age < timedelta(minutes=-5):
        return "FUTURE"
    max_age = int(getattr(settings, "lingxing_performance_max_age_hours", 8))
    return "STALE" if age > timedelta(hours=max_age) else "FRESH"


def _numeric(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "—", "N/A", "n/a", "null", "None"}:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except ValueError:
        return None


def _threshold_value(value):
    if isinstance(value, dict):
        for key in ("standard", "value", "threshold", "target"):
            if key in value:
                return _numeric(value[key])
        return None
    return _numeric(value)


def _standard_map(standard):
    result = {}
    if isinstance(standard, dict):
        for key, value in standard.items():
            numeric = _threshold_value(value)
            if numeric is not None:
                result[_normalize_key(key)] = numeric
    elif isinstance(standard, list):
        for item in standard:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("indicator") or item.get("key") or item.get("field")
                or item.get("name") or item.get("code")
            )
            if key is None:
                continue
            numeric = _threshold_value(item)
            if numeric is not None:
                result[_normalize_key(key)] = numeric
    return result


def _standard_for(standards, field):
    normalized = _normalize_key(field)
    if normalized in standards:
        return standards[normalized]
    camel = _normalize_key(field.replace("_", ""))
    return standards.get(camel)


def _upsert_sync_state(db, *, store_id, dataset, status, source_updated_at,
                       record_count, error_message=None):
    row = db.scalar(select(PerformanceSyncState).where(
        PerformanceSyncState.store_id == store_id,
        PerformanceSyncState.dataset == dataset,
    ))
    if row is None:
        row = PerformanceSyncState(store_id=store_id, dataset=dataset)
        db.add(row)
    row.status = status
    row.source_updated_at = _timestamp_text(source_updated_at)
    row.captured_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.record_count = int(record_count)
    row.error_message = error_message
    return row


def _queue_freshness_event(db, store_id, dataset, status, source_timestamp):
    scope = f"{store_id}:performance_freshness:{dataset}"
    if status == "FRESH":
        saved = upsert_anomalies(
            db, store_id, [], evaluation_scope=scope, scope_complete=True
        )
    else:
        severity = "P1" if status == "STALE" else "P2"
        message = {
            "STALE": "领星绩效数据源更新时间已超过允许时限",
            "MISSING": "领星绩效响应缺少数据源更新时间",
            "NOT_CONFIGURED": "未配置绩效源时间时区，无法验证数据新鲜度",
            "FUTURE": "领星绩效源更新时间晚于当前时间",
            "FAILED": "领星绩效接口采集失败",
        }.get(status, "领星绩效数据新鲜度未知")
        event = {
            "rule": f"performance_{dataset}_freshness",
            "severity": severity,
            "entity_key": dataset,
            "dedupe_key": f"performance:{dataset}:freshness",
            "metrics": {
                "dataset": dataset,
                "freshness_status": status,
                "source_updated_at": _timestamp_text(source_timestamp),
            },
            "reason": message,
        }
        saved = upsert_anomalies(
            db, store_id, [event], evaluation_scope=scope, scope_complete=False
        )
    enqueue_anomaly_notifications(db, saved)


def _build_performance_events(store_id, store_name, row, standards, freshness):
    events = []
    status = row.get("ahr_status")
    status_text = str(status or "").strip()
    normalized_status = _normalize_key(status_text)
    if normalized_status in _RISK_STATUSES or status_text in _RISK_STATUSES:
        severe = any(term in normalized_status for term in ("critical", "suspended", "deactivated"))
        events.append({
            "rule": "account_health_at_risk",
            "severity": "P0" if severe else "P1",
            "entity_key": store_id,
            "dedupe_key": f"performance:ahr_status:{row.get('pull_date')}",
            "metrics": {
                "ahr_score": _numeric(row.get("ahr_score")),
                "ahr_status": status_text,
                "pull_date": row.get("pull_date"),
                "source_updated_at": row.get("update_date"),
            },
            "reason": f"店铺「{store_name}」账户健康状态为「{status_text}」，需要立即检查",
        })

    breaches = []
    for field, direction in INDICATOR_DIRECTIONS.items():
        actual = _numeric(row.get(field))
        standard = _standard_for(standards, field)
        if actual is None or standard is None:
            continue
        breached = actual > standard if direction == "high" else actual < standard
        if not breached:
            continue
        breach = {
            "indicator": field,
            "value": actual,
            "standard": standard,
            "direction": direction,
        }
        breaches.append(breach)
        events.append({
            "rule": "performance_indicator_breached",
            "severity": "P1",
            "entity_key": store_id,
            "dedupe_key": f"performance:indicator:{row.get('pull_date')}:{field}",
            "metrics": {
                **breach,
                "ahr_score": _numeric(row.get("ahr_score")),
                "ahr_status": status_text or None,
                "pull_date": row.get("pull_date"),
                "source_updated_at": row.get("update_date"),
            },
            "reason": (
                f"店铺「{store_name}」绩效指标 {field}={actual}，"
                f"已违反领星返回的平台标准 {standard}"
            ),
        })
    return events, breaches


def _notice_identity(row):
    for key in ("notice_id", "id", "webmail_uuid", "message_id", "uuid"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)[:200]
    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _notice_status(value):
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None, "UNKNOWN"
    return code, _STATUS_LABELS.get(code, "UNKNOWN")


def _save_notice(db, store_id, row, source_updated_at):
    external_id = _notice_identity(row)
    notice_key = sha256(f"{store_id}:{external_id}".encode("utf-8")).hexdigest()
    status_code, status_label = _notice_status(row.get("status"))
    subject = row.get("subject") or row.get("title") or row.get("mail_subject")
    occurred_at = (
        row.get("date") or row.get("create_time") or row.get("createTime")
        or row.get("occurred_at") or row.get("notice_time")
    )
    is_read = row.get("isRead")
    try:
        is_read = int(is_read) if is_read is not None else None
    except (TypeError, ValueError):
        is_read = None

    saved = db.scalar(select(PerformanceNotice).where(
        PerformanceNotice.store_id == store_id,
        PerformanceNotice.notice_key == notice_key,
    ))
    if saved is None:
        saved = PerformanceNotice(
            store_id=store_id,
            notice_key=notice_key,
            first_seen=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(saved)
    saved.external_id = external_id
    saved.subject = str(subject)[:4000] if subject is not None else None
    saved.status_code = status_code
    saved.status_label = status_label
    saved.occurred_at = _timestamp_text(occurred_at)
    saved.is_read = is_read
    saved.source_updated_at = _timestamp_text(source_updated_at)
    saved.payload = row
    saved.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)

    scope = f"{store_id}:performance_notice:{notice_key}"
    if status_code == 1:
        title = str(subject or "未命名业绩通知")
        event = {
            "rule": "performance_notice_pending",
            "severity": "P1",
            "entity_key": notice_key,
            "dedupe_key": f"performance_notice:{notice_key}",
            "metrics": {
                "notice_id": external_id,
                "subject": title,
                "status_code": status_code,
                "status_label": status_label,
                "occurred_at": _timestamp_text(occurred_at),
                "is_read": is_read,
            },
            "reason": f"店铺「{store_id}」存在待处理业绩通知：{title}",
        }
        anomalies = upsert_anomalies(
            db, store_id, [event], evaluation_scope=scope, scope_complete=False
        )
        enqueue_anomaly_notifications(db, anomalies)
    elif status_code in {2, 3}:
        # Only explicit handled/no-action states close a pending notice.
        anomalies = upsert_anomalies(
            db, store_id, [], evaluation_scope=scope, scope_complete=True
        )
        enqueue_anomaly_notifications(db, anomalies)
    # Status 0 and unknown values remain UNKNOWN; they never recover an alert.


def _notice_last_update(response):
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict) and data.get("lastUpdateDate") is not None:
        return data.get("lastUpdateDate")
    return response.get("lastUpdateDate") if isinstance(response, dict) else None


def _store_performance_for_sid(client, db, sid, store_name, settings):
    store_id = str(sid)
    page_size = int(settings.lingxing_performance_page_size)
    max_pages = int(settings.lingxing_performance_max_pages)

    rows, pages = _fetch_all(
        lambda offset, length: client.store_performance_list({
            "sids": store_id, "offset": offset, "length": length,
        }),
        page_size=page_size,
        max_pages=max_pages,
        label="Store performance list",
    )
    for offset, response in pages:
        db.add(RawSnapshot(
            source="lingxing_store_performance_list",
            store_id=store_id,
            payload={"offset": offset, "response": response},
        ))

    if not rows:
        _upsert_sync_state(
            db, store_id=store_id, dataset="store_performance", status="MISSING",
            source_updated_at=None, record_count=0,
        )
        _queue_freshness_event(db, store_id, "store_performance", "MISSING", None)
    else:
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Store performance list contains a non-object row")
            pull_date = row.get("pull_date")
            if not pull_date:
                raise RuntimeError("Store performance row is missing pull_date")
            try:
                parsed_date = date.fromisoformat(str(pull_date)[:10])
            except ValueError as exc:
                raise RuntimeError("Store performance pull_date is invalid") from exc
            normalized.append((parsed_date, row))
        pull_date, list_row = max(normalized, key=lambda item: item[0])
        detail_response = client.store_performance_detail({
            "sid": _sid_value(sid),
            "pullDate": pull_date.isoformat(),
        })
        db.add(RawSnapshot(
            source="lingxing_store_performance_detail",
            store_id=store_id,
            payload={"sid": _sid_value(sid), "pullDate": pull_date.isoformat(),
                     "response": detail_response},
        ))
        detail_data = detail_response.get("data")
        if not isinstance(detail_data, dict):
            raise RuntimeError("Store performance detail data must be a JSON object")
        raw_standard = detail_data.get("standard")
        standards = _standard_map(raw_standard)
        freshness = _freshness_status(list_row.get("update_date"), settings)
        indicators = {
            field: list_row.get(field) for field in INDICATOR_DIRECTIONS
            if field in list_row
        }
        events, breaches = _build_performance_events(
            store_id, store_name, list_row, standards, freshness
        )

        saved = db.scalar(select(StorePerformance).where(
            StorePerformance.store_id == store_id,
            StorePerformance.pull_date == pull_date,
        ))
        if saved is None:
            saved = StorePerformance(store_id=store_id, pull_date=pull_date)
            db.add(saved)
        saved.source_updated_at = _timestamp_text(list_row.get("update_date"))
        saved.captured_at = datetime.now(timezone.utc).replace(tzinfo=None)
        saved.ahr_score = _numeric(list_row.get("ahr_score"))
        saved.ahr_status = str(list_row.get("ahr_status")) if list_row.get("ahr_status") is not None else None
        saved.indicators = indicators
        saved.standards = raw_standard if isinstance(raw_standard, dict) else {"items": raw_standard or []}
        saved.breaches = breaches
        saved.detail = detail_data
        saved.freshness_status = freshness

        comparable_fields = [
            field for field in INDICATOR_DIRECTIONS
            if list_row.get(field) is not None
        ]
        standards_complete = bool(comparable_fields) and all(
            _standard_for(standards, field) is not None for field in comparable_fields
        )
        status_text = _normalize_key(list_row.get("ahr_status") or "")
        status_known = status_text in _NORMAL_STATUSES or status_text in _RISK_STATUSES
        scope_complete = freshness == "FRESH" and standards_complete and status_known
        anomalies = upsert_anomalies(
            db, store_id, events,
            evaluation_scope=f"{store_id}:store_performance",
            scope_complete=scope_complete,
        )
        enqueue_anomaly_notifications(db, anomalies)
        _upsert_sync_state(
            db, store_id=store_id, dataset="store_performance",
            status=freshness, source_updated_at=list_row.get("update_date"),
            record_count=len(rows),
        )
        _queue_freshness_event(
            db, store_id, "store_performance", freshness, list_row.get("update_date")
        )

    notice_rows, notice_pages = _fetch_all(
        lambda offset, length: client.performance_notice_list({
            "sid": _sid_value(sid), "offset": offset, "length": length,
        }),
        page_size=page_size,
        max_pages=max_pages,
        label="Performance notice list",
    )
    source_updated_at = None
    for offset, response in notice_pages:
        if source_updated_at is None:
            source_updated_at = _notice_last_update(response)
        db.add(RawSnapshot(
            source="lingxing_performance_notice_list",
            store_id=store_id,
            payload={"offset": offset, "response": response},
        ))
    for row in notice_rows:
        if not isinstance(row, dict):
            raise RuntimeError("Performance notice list contains a non-object row")
        _save_notice(db, store_id, row, source_updated_at)

    notice_freshness = _freshness_status(source_updated_at, settings)
    _upsert_sync_state(
        db, store_id=store_id, dataset="performance_notices",
        status=notice_freshness, source_updated_at=source_updated_at,
        record_count=len(notice_rows),
    )
    _queue_freshness_event(
        db, store_id, "performance_notices", notice_freshness, source_updated_at
    )
    db.flush()
    return {
        "store_id": store_id,
        "performance_rows": len(rows),
        "notice_rows": len(notice_rows),
        "performance_freshness": (
            freshness if rows else "MISSING"
        ),
        "notice_freshness": notice_freshness,
    }


def collect_store_performance(client, db, *, stores_response, settings):
    """Collect latest performance snapshot and all notice pages for each store.

    The notice request intentionally omits status/isRead/date filters so an
    incomplete filter can never hide a pending notice.
    """
    store_rows = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(store_rows, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")
    summary = {
        "status": "SUCCEEDED",
        "stores_seen": len(store_rows),
        "performance_rows": 0,
        "notice_rows": 0,
        "freshness": [],
        "errors": [],
    }
    for store in store_rows:
        sid = store.get("sid") if isinstance(store, dict) else None
        if sid is None:
            summary["errors"].append("seller-list row missing sid")
            continue
        store_id = str(sid)
        try:
            result = _store_performance_for_sid(
                client, db, sid, str(store.get("name") or sid), settings
            )
            summary["performance_rows"] += result["performance_rows"]
            summary["notice_rows"] += result["notice_rows"]
            summary["freshness"].append(result)
            if result["performance_freshness"] != "FRESH" or result["notice_freshness"] != "FRESH":
                if summary["status"] == "SUCCEEDED":
                    summary["status"] = "SUCCEEDED_WITH_WARNINGS"
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            summary["errors"].append({"store_id": store_id, "error": message})
            for dataset in ("store_performance", "performance_notices"):
                _upsert_sync_state(
                    db, store_id=store_id, dataset=dataset, status="FAILED",
                    source_updated_at=None, record_count=0, error_message=message,
                )
                _queue_freshness_event(db, store_id, dataset, "FAILED", None)
            summary["status"] = "PARTIAL_FAILED"
            db.flush()
    db.commit()
    return summary
