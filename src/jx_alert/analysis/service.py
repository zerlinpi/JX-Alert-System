from datetime import datetime
from sqlalchemy import select
from jx_alert.storage.models import AnomalyEvent


_SEVERITY_ORDER = {"P1": 1, "P2": 2, "P3": 3}


def upsert_anomalies(db, store_id: str, events: list[dict], *,
                     evaluation_scope: str, scope_complete: bool = False
                     ) -> list[AnomalyEvent]:
    """Upsert one complete evaluation scope; only a complete scope can recover old events.

    evaluation_scope should identify the same store + metric family/date window each run.
    If the collector failed or pagination was incomplete, pass scope_complete=False.
    """
    if not evaluation_scope:
        raise ValueError("evaluation_scope is required")
    saved = []
    now = datetime.utcnow()
    active_keys = {event["dedupe_key"] for event in events}

    for event in events:
        row = db.scalar(
            select(AnomalyEvent).where(
                AnomalyEvent.dedupe_key == event["dedupe_key"]
            )
        )
        if row:
            previous_severity = row.severity
            was_recovered = row.status == "RECOVERED"
            row.status = "NEW" if was_recovered else "ONGOING"
            if (_SEVERITY_ORDER.get(event["severity"], 99)
                    < _SEVERITY_ORDER.get(previous_severity, 99)):
                row.status = "ESCALATED"
            row.last_seen = now
            row.severity = event["severity"]
            row.metrics = event["metrics"]
            row.reason = event["reason"]
            row.evaluation_scope = evaluation_scope
            row.recovered_at = None
        else:
            row = AnomalyEvent(
                dedupe_key=event["dedupe_key"],
                store_id=store_id,
                rule=event["rule"],
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
        stale_rows = db.scalars(
            select(AnomalyEvent).where(
                AnomalyEvent.store_id == store_id,
                AnomalyEvent.evaluation_scope == evaluation_scope,
                AnomalyEvent.status.in_(["NEW", "ONGOING", "ESCALATED"]),
                AnomalyEvent.dedupe_key.not_in(active_keys) if active_keys else True,
            )
        ).all()
        for row in stale_rows:
            row.status = "RECOVERED"
            row.recovered_at = now
            row.last_seen = now

    return saved
