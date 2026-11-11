from datetime import datetime
from sqlalchemy import select
from jx_alert.storage.models import AnomalyEvent

def upsert_anomalies(db, store_id: str, events: list[dict]) -> list[AnomalyEvent]:
    """Persist event lifecycle with a stable dedupe key; caller owns transaction."""
    saved = []
    now = datetime.utcnow()
    active_keys = {e["dedupe_key"] for e in events}
    for event in events:
        row = db.scalar(select(AnomalyEvent).where(AnomalyEvent.dedupe_key == event["dedupe_key"]))
        if row:
            row.status = "ONGOING"
            row.last_seen = now
            row.severity = event["severity"]
            row.metrics = event["metrics"]
            row.reason = event["reason"]
        else:
            row = AnomalyEvent(dedupe_key=event["dedupe_key"], store_id=store_id,
                rule=event["rule"], severity=event["severity"], status="NEW",
                first_seen=now, last_seen=now, metrics=event["metrics"], reason=event["reason"])
            db.add(row)
        saved.append(row)
    # Recovery is only safe when caller supplies a complete evaluation scope.
    return saved
