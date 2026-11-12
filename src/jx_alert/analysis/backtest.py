"""Replay real stored history in an isolated warehouse; never send or alter source data."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    Store, Product, Campaign, AdProduct, DailyMetric, DataCoverage, AnomalyEvent,
)


def backtest_history(db, *, store_id: str, start_date: str, end_date: str,
                     labels: dict | None = None) -> dict:
    """Use the production evaluator on copied facts, with separate anomaly/outbox tables.

    labels maps date|store|rule|entity to true_positive/false_positive. No false
    positive rate is inferred without those human-reviewed labels. Current stored
    historical revisions are replayed; this is not an event-time simulation.
    """
    from jx_alert.analysis.evaluator import evaluate_daily_anomalies
    from jx_alert.notifications.outbox import NotificationOutbox  # noqa: F401
    first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if not 0 <= (last - first).days <= 30:
        raise ValueError("Backtest window must contain 1-31 dates")
    source_store = db.get(Store, str(store_id))
    if source_store is None or not source_store.timezone:
        raise ValueError("A store with a verified marketplace timezone is required")
    from zoneinfo import ZoneInfo
    if last >= datetime.now(ZoneInfo(source_store.timezone)).date():
        raise ValueError("Only completed marketplace-local dates may be backtested")
    labels = labels or {}
    if any(value not in {"true_positive", "false_positive"} for value in labels.values()):
        raise ValueError("Review labels must be true_positive or false_positive")
    temporary = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(temporary)
    candidates, evaluations = [], []
    with Session(temporary) as isolated:
        isolated.add(Store(**{c.name: getattr(source_store, c.name)
                              for c in Store.__table__.columns}))
        for model in (Product, Campaign, AdProduct, DailyMetric, DataCoverage):
            query = select(model).where(model.store_id == str(store_id))
            if model in (DailyMetric, DataCoverage):
                query = query.where(model.metric_date >= first - timedelta(days=22),
                                    model.metric_date <= last)
            for row in db.scalars(query):
                isolated.add(model(**{c.name: getattr(row, c.name) for c in model.__table__.columns}))
        isolated.commit()
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            started = datetime.now(timezone.utc).replace(tzinfo=None)
            evaluation = evaluate_daily_anomalies(isolated, {str(store_id): day.isoformat()})
            evaluations.append({"report_date": day.isoformat(), **evaluation})
            for event in isolated.scalars(select(AnomalyEvent).where(
                AnomalyEvent.last_seen >= started,
                AnomalyEvent.status.in_(["NEW", "ONGOING", "ESCALATED"]),
            )):
                key = f"{day}:{store_id}:{event.rule}:{event.entity_id}"
                candidates.append({
                    "key": key, "report_date": day.isoformat(), "store_id": str(store_id),
                    "entity_id": event.entity_id, "rule": event.rule,
                    "severity": event.severity, "metrics": event.metrics,
                    "reason": event.reason, "review_label": labels.get(key),
                })
    temporary.dispose()
    tp = sum(r["review_label"] == "true_positive" for r in candidates)
    fp = sum(r["review_label"] == "false_positive" for r in candidates)
    return {
        "store_id": str(store_id), "start_date": start_date, "end_date": end_date,
        "candidate_count": len(candidates), "candidates": candidates,
        "evaluations": evaluations, "true_positive": tp, "false_positive": fp,
        "unreviewed": len(candidates) - tp - fp,
        "false_positive_rate": fp / (tp + fp) if tp + fp else None,
        "replay_basis": "Current stored historical revisions; no event-time finality inferred",
        "notification_policy": "Isolated outbox only; no external delivery",
    }
