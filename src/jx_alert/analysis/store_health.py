"""Read-only account-health observations; never create or close business alerts."""
from datetime import datetime, timezone
import math

from sqlalchemy import func, select

from jx_alert.lingxing.performance_openapi import (
    _freshness_status, _normalize_key, _numeric, _standard_for, _standard_map,
)
from jx_alert.storage.models import PerformanceNotice, PerformanceSyncState, StorePerformance


INDICATORS = {
    "order_with_defect": ("订单缺陷率", "high"),
    "late_shipment": ("迟发率", "high"),
    "pre_fulfillment_cancellation": ("配送前取消率", "high"),
    "valid_tracking": ("有效追踪率", "low"),
    "on_time_delivery": ("准时送达率", "low"),
    "fba_order_with_defect": ("FBA订单缺陷率", "high"),
    "invoice_defect": ("发票缺陷率", "high"),
}
STATUS_LABELS = {
    "HEALTHY": "健康", "ATTENTION": "需关注", "UNCONFIRMED": "待确认",
    "NO_DATA": "无绩效数据", "AUTHORIZATION_FAILED": "店铺授权异常", "INACTIVE": "店铺未启用",
}
FRESHNESS_LABELS = {
    "FRESH": "源数据有效", "NOT_CONFIGURED": "源时区待确认", "STALE": "源数据过期",
    "FUTURE": "源时间异常", "MISSING": "缺少源数据或更新时间", "FAILED": "采集失败",
    "UNKNOWN": "数据状态待确认",
}
_NORMAL = {"healthy", "normal", "good", "正常", "健康", "健康正常"}
_SOURCE_LABELS = {value: "健康" for value in _NORMAL}
_SOURCE_LABELS.update({
    "atrisk": "有风险", "risk": "有风险", "critical": "严重风险", "unhealthy": "不健康",
    "suspended": "已停用", "deactivated": "已停用", "有风险": "有风险", "风险": "有风险",
    "严重": "严重风险", "不健康": "不健康", "停用": "已停用", "停权": "已停用",
    "great": "评级良好", "notapplicable": "不适用", "不适用": "不适用",
})
_RISK = {key for key in _SOURCE_LABELS if key not in _NORMAL | {"great", "notapplicable", "不适用"}}


def _clock(now):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Health snapshots require an aware clock")
    return value.astimezone(timezone.utc)


def _utc(value):
    if value is None:
        return None
    # Warehouse capture timestamps are stored as naive UTC, unlike source time.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _finite(value):
    try:
        numeric = _numeric(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if numeric is not None and math.isfinite(numeric) and numeric >= 0 else None


def _rate(value):
    numeric = _finite(value)
    return numeric if numeric is not None and numeric <= 100 else None


def _freshness(source_updated_at, settings, clock, sync, captured_at=None):
    status = _freshness_status(source_updated_at, settings, now=clock)
    if sync is not None and sync.status in {"FAILED", "MISSING"}:
        # A newer failed/empty request cannot make the last healthy observation current.
        if captured_at is None or _utc(sync.captured_at) is None or _utc(sync.captured_at) >= _utc(captured_at):
            return sync.status
    return status


def _indicator_rows(performance):
    observed = performance.indicators if performance is not None and isinstance(performance.indicators, dict) else {}
    raw_standards = performance.standards if performance is not None else {}
    if isinstance(raw_standards, dict) and "items" in raw_standards:
        raw_standards = raw_standards["items"]
    try:
        standards = _standard_map(raw_standards)
    except (TypeError, ValueError, OverflowError):
        standards = {}
    rows, issues = [], []
    for field, (label, direction) in INDICATORS.items():
        actual, standard = _rate(observed.get(field)), _rate(_standard_for(standards, field))
        status, status_label = "INSUFFICIENT_DATA", "数据不足"
        if actual is None:
            status_label = "未提供观测值" if observed.get(field) is None else "观测值无效"
        elif standard is None:
            status_label = "平台标准缺失或无效"
        elif actual == standard:
            # The API's bare number does not establish a strict/inclusive operator.
            status, status_label = "AT_THRESHOLD", "达到标准边界，待核查"
        elif actual > standard if direction == "high" else actual < standard:
            status, status_label = "BREACHED", "观测未达标"
            relation = "高于" if direction == "high" else "低于"
            issues.append(f"{label}{actual:g}%{relation}平台标准{standard:g}%")
        else:
            status, status_label = "WITHIN_STANDARD", "未见超标"
        rows.append({"field": field, "label": label, "value": actual, "standard": standard,
                     "status": status, "status_label": status_label})
    return rows, issues


def _indicator_summary(rows):
    breached = [item["label"] for item in rows if item["status"] == "BREACHED"]
    boundary = [item["label"] for item in rows if item["status"] == "AT_THRESHOLD"]
    missing = sum(item["status"] == "INSUFFICIENT_DATA" for item in rows)
    parts = []
    if breached:
        parts.append("观测未达标：" + "、".join(breached))
    if boundary:
        parts.append("标准边界待核查：" + "、".join(boundary))
    if not parts and missing < len(rows):
        parts.append("可比指标未见超标" if missing else "未见超标")
    if missing:
        parts.append(f"{missing}项数据不足")
    return "；".join(parts)


def store_health_snapshot(db, store, settings, *, now=None):
    """Return each source observation and its current reliability without writes.

    All rates use the API's percentage-point scale. Source ratings such as GREAT
    remain observations; neither AHR score nor absent breaches imply health.
    """
    clock = _clock(now)
    with db.no_autoflush:
        performance = db.scalar(select(StorePerformance).where(StorePerformance.store_id == store.sid)
            .order_by(StorePerformance.pull_date.desc(), StorePerformance.captured_at.desc(), StorePerformance.id.desc()).limit(1))
        sync = {row.dataset: row for row in db.scalars(select(PerformanceSyncState).where(
            PerformanceSyncState.store_id == store.sid))}
        pending = db.scalar(select(func.count(PerformanceNotice.id)).where(
            PerformanceNotice.store_id == store.sid, PerformanceNotice.status_label == "PENDING"))
    performance_sync = sync.get("store_performance")
    source_updated_at = performance.source_updated_at if performance is not None else None
    freshness = (_freshness(source_updated_at, settings, clock, performance_sync,
                           performance.captured_at if performance is not None else None)
                 if performance is not None else
                 "FAILED" if performance_sync is not None and performance_sync.status == "FAILED" else "MISSING")
    notice_sync = sync.get("performance_notices")
    notice_freshness = (_freshness(notice_sync.source_updated_at, settings, clock, notice_sync)
                        if notice_sync is not None else "MISSING")
    raw_status = performance.ahr_status if performance is not None else None
    source_status = _normalize_key(raw_status or "")
    source_label = _SOURCE_LABELS.get(source_status, "未识别" if source_status else "未提供")
    indicators, issues = _indicator_rows(performance)
    status = "UNCONFIRMED"
    if store.status == 2:
        status = "AUTHORIZATION_FAILED"
    elif store.status in {0, 3}:
        status = "INACTIVE"
    elif performance is None:
        status = "NO_DATA"
    elif store.status == 1 and freshness == "FRESH":
        if source_status in _RISK or issues:
            status = "ATTENTION"
        elif source_status in _NORMAL and all(row["status"] == "WITHIN_STANDARD" for row in indicators):
            status = "HEALTHY"
    captured_at = _utc(performance.captured_at) if performance is not None else None
    if status != "UNCONFIRMED":
        reason = STATUS_LABELS[status]
    elif freshness != "FRESH":
        reason = FRESHNESS_LABELS.get(freshness, FRESHNESS_LABELS["UNKNOWN"])
    elif source_status not in _NORMAL | _RISK:
        reason = "源评级口径待核实"
    else:
        reason = _indicator_summary(indicators)
    return {
        "store_id": str(store.sid), "store_name": store.name,
        "status": status, "status_label": STATUS_LABELS[status],
        "confirmation_reason": reason,
        "source_status_label": source_label,
        "ahr_score": _finite(performance.ahr_score) if performance is not None else None,
        "pull_date": performance.pull_date.isoformat() if performance is not None else None,
        "source_updated_at": source_updated_at,
        "captured_at": captured_at.isoformat() if captured_at is not None else None,
        "freshness_status": freshness, "freshness_label": FRESHNESS_LABELS.get(freshness, FRESHNESS_LABELS["UNKNOWN"]),
        "indicators": indicators, "issues": issues, "indicator_summary": _indicator_summary(indicators),
        "pending_notice_observations": int(pending or 0),
        "notice_freshness_status": notice_freshness,
        "notice_freshness_label": FRESHNESS_LABELS.get(notice_freshness, FRESHNESS_LABELS["UNKNOWN"]),
    }
