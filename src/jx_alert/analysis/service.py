import json
from datetime import datetime, timezone

from sqlalchemy import select

from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.storage.models import AnomalyEvent


_SEVERITY_ORDER = {"P1": 1, "P2": 2, "P3": 3}


def _scoped_dedupe_key(store_id: str, event_key: str) -> str:
    prefix = f"{store_id}:"
    return event_key if event_key.startswith(prefix) else prefix + event_key


def upsert_anomalies(db, store_id: str, events: list[dict], *,
                     evaluation_scope: str, scope_complete: bool = False
                     ) -> list[AnomalyEvent]:
    """Upsert one complete evaluation scope; only a complete scope can recover old events.

    evaluation_scope should identify the same store + metric family/date window each run.
    If collection failed or pagination was incomplete, pass scope_complete=False.
    """
    if not evaluation_scope:
        raise ValueError("evaluation_scope is required")
    saved = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    active_keys = {
        _scoped_dedupe_key(store_id, event["dedupe_key"]) for event in events
    }

    for event in events:
        dedupe_key = _scoped_dedupe_key(store_id, event["dedupe_key"])
        row = db.scalar(
            select(AnomalyEvent).where(AnomalyEvent.dedupe_key == dedupe_key)
        )
        if row:
            previous_severity = row.severity
            was_recovered = row.status == "RECOVERED"
            row.status = "NEW" if was_recovered else "ONGOING"
            if (_SEVERITY_ORDER.get(event["severity"], 99)
                    < _SEVERITY_ORDER.get(previous_severity, 99)):
                row.status = "ESCALATED"
            if was_recovered:
                row.first_seen = now
            row.last_seen = now
            row.severity = event["severity"]
            row.entity_id = event.get("entity_key", "")
            row.metrics = event["metrics"]
            row.reason = event["reason"]
            row.evaluation_scope = evaluation_scope
            row.recovered_at = None
        else:
            row = AnomalyEvent(
                dedupe_key=dedupe_key,
                store_id=store_id,
                rule=event["rule"],
                entity_id=event.get("entity_key", ""),
                severity=event["severity"],
                status="NEW",
                evaluation_scope=evaluation_scope,
                first_seen=now,
                last_seen=now,
                metrics=event["metrics"],
                reason=event["reason"],
            )
            db.add(row)
        saved.append(row)

    if scope_complete:
        conditions = [
            AnomalyEvent.store_id == store_id,
            AnomalyEvent.evaluation_scope == evaluation_scope,
            AnomalyEvent.status.in_(["NEW", "ONGOING", "ESCALATED"]),
        ]
        if active_keys:
            conditions.append(AnomalyEvent.dedupe_key.not_in(active_keys))
        recovered_rows = db.scalars(select(AnomalyEvent).where(*conditions)).all()
        for row in recovered_rows:
            row.status = "RECOVERED"
            row.recovered_at = now
            row.last_seen = now
            saved.append(row)

    db.flush()
    return saved


def enqueue_anomaly_notifications(db, anomalies: list[AnomalyEvent]) -> int:
    """Queue first-seen, escalated and recovered events; ongoing events stay quiet."""
    queued = 0
    for anomaly in anomalies:
        if anomaly.status not in {"NEW", "ESCALATED", "RECOVERED"}:
            continue
        state_label = {
            "NEW": "新异常",
            "ESCALATED": "异常升级",
            "RECOVERED": "异常恢复",
        }[anomaly.status]
        metrics = json.dumps(anomaly.metrics or {}, ensure_ascii=False, default=str)
        message = (
            f"【JX Alert】{state_label} · {anomaly.severity}\n"
            f"店铺 ID：{anomaly.store_id}\n"
            f"对象：{anomaly.entity_id or '未指定'}\n"
            f"规则：{anomaly.rule}\n"
            f"说明：{anomaly.reason}\n"
            f"指标：{metrics}"
        )
        first_seen = anomaly.first_seen.isoformat() if anomaly.first_seen else "unknown"
        enqueue_notification(
            db,
            idempotency_key=(
                f"anomaly:{anomaly.dedupe_key}:{anomaly.status}:"
                f"{anomaly.severity}:{first_seen}"
            ),
            text=message,
            store_id=anomaly.store_id,
            entity_id=anomaly.entity_id or None,
        )
        queued += 1
    return queued
