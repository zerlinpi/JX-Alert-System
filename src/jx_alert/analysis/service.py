import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from jx_alert.config import get_settings
from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.storage.models import AnomalyEvent, Campaign, Product, Store


_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_ESCALATED_SEVERITY = {"P3": "P2", "P2": "P1", "P1": "P1", "P0": "P0"}


def _scoped_dedupe_key(store_id: str, event_key: str) -> str:
    prefix = f"{store_id}:"
    return event_key if event_key.startswith(prefix) else prefix + event_key


def upsert_anomalies(db, store_id: str, events: list[dict], *,
                     evaluation_scope: str, scope_complete: bool = False,
                     recovery_entity_ids: set[str] | None = None,
                     escalation_after_days: int = 2) -> list[AnomalyEvent]:
    """Upsert one complete evaluation scope; only a complete scope can recover old events.

    evaluation_scope should identify the same store + metric family/date window each run.
    If collection failed or pagination was incomplete, pass scope_complete=False.
    When recovery_entity_ids is provided, only those fully evaluated entities may recover;
    an empty set intentionally recovers none. None preserves scope_complete behavior.
    """
    if not evaluation_scope:
        raise ValueError("evaluation_scope is required")
    if escalation_after_days < 1:
        raise ValueError("escalation_after_days must be at least 1")
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
            incoming_severity = event["severity"]
            if (
                not was_recovered
                and row.escalated_at is not None
                and _SEVERITY_ORDER.get(previous_severity, 99)
                < _SEVERITY_ORDER.get(incoming_severity, 99)
            ):
                incoming_severity = previous_severity
            severity_increased = (
                _SEVERITY_ORDER.get(incoming_severity, 99)
                < _SEVERITY_ORDER.get(previous_severity, 99)
            )
            persisted_long_enough = (
                not was_recovered
                and row.escalated_at is None
                and now - row.first_seen >= timedelta(days=escalation_after_days)
            )
            if was_recovered:
                row.status = "NEW"
                row.first_seen = now
                row.escalated_at = None
                row.severity = incoming_severity
            elif severity_increased:
                row.status = "ESCALATED"
                row.escalated_at = now
                row.severity = incoming_severity
            elif persisted_long_enough:
                row.status = "ESCALATED"
                row.escalated_at = now
                row.severity = _ESCALATED_SEVERITY.get(incoming_severity, incoming_severity)
            else:
                row.status = "ONGOING"
                row.severity = incoming_severity
            row.last_seen = now
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

    if scope_complete or recovery_entity_ids is not None:
        conditions = [
            AnomalyEvent.store_id == store_id,
            AnomalyEvent.evaluation_scope == evaluation_scope,
            AnomalyEvent.status.in_(["NEW", "ONGOING", "ESCALATED"]),
        ]
        if recovery_entity_ids is not None:
            eligible_ids = {str(entity_id) for entity_id in recovery_entity_ids}
            conditions.append(AnomalyEvent.entity_id.in_(eligible_ids))
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


def _notification_labels(db, anomaly: AnomalyEvent) -> tuple[str, str | None, str | None]:
    store = db.get(Store, anomaly.store_id)
    store_label = (
        f"{store.name}（{anomaly.store_id}）" if store else anomaly.store_id
    )
    metrics = anomaly.metrics or {}

    campaign_id = metrics.get("campaign_id")
    if not campaign_id and ":campaign:" in (anomaly.evaluation_scope or ""):
        campaign_id = anomaly.entity_id
    campaign_label = None
    if campaign_id is not None:
        campaign = db.scalar(select(Campaign).where(
            Campaign.store_id == anomaly.store_id,
            Campaign.campaign_id == str(campaign_id),
        ).limit(1))
        if campaign:
            campaign_label = (
                f"{campaign.name or campaign.campaign_id} "
                f"（{campaign.campaign_type.upper()} / ID {campaign.campaign_id}）"
            )
        else:
            campaign_label = f"活动 ID {campaign_id}"

    product_label = None
    product_key = (
        metrics.get("seller_sku")
        or metrics.get("asin")
        or anomaly.entity_id
    )
    if product_key:
        products = db.scalars(select(Product).where(
            Product.store_id == anomaly.store_id,
            or_(
                Product.seller_sku == str(product_key),
                Product.asin == str(product_key),
            ),
        ).order_by(Product.seller_sku)).all()
        if products:
            product_label = "; ".join(
                f"MSKU {product.seller_sku}"
                + (f" / ASIN {product.asin}" if product.asin else "")
                + (f" / Local SKU {product.local_sku}" if product.local_sku else "")
                for product in products
            )
        elif metrics.get("asin") or metrics.get("seller_sku"):
            product_label = (
                f"MSKU {metrics.get('seller_sku') or '-'}"
                f" / ASIN {metrics.get('asin') or '-'}"
            )

    return store_label, campaign_label, product_label


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
        store_label, campaign_label, product_label = _notification_labels(db, anomaly)
        lines = [
            f"【JX Alert】{state_label} · {anomaly.severity}",
            f"店铺：{store_label}",
        ]
        if campaign_label:
            lines.append(f"广告活动：{campaign_label}")
        if product_label:
            lines.append(f"商品：{product_label}")
        elif not campaign_label and anomaly.entity_id:
            lines.append(f"监控对象：{anomaly.entity_id}")
        lines.extend([
            f"规则：{anomaly.rule}",
            f"说明：{anomaly.reason}",
            f"指标：{metrics}",
        ])
        message = "\n".join(lines)
        first_seen = anomaly.first_seen.isoformat() if anomaly.first_seen else "unknown"
        event_key = (
            f"anomaly:{anomaly.dedupe_key}:{anomaly.status}:"
            f"{anomaly.severity}:{first_seen}"
        )
        settings = get_settings()
        if getattr(settings, "feishu_transport", "webhook") == "app":
            from jx_alert.notifications.routing import enqueue_application_deliveries
            result = enqueue_application_deliveries(
                db, settings, event_key=event_key, text=message,
                context={
                    "store_id": anomaly.store_id, "entity_id": anomaly.entity_id,
                    "metrics": anomaly.metrics or {}, "scope": anomaly.evaluation_scope,
                    "severity": anomaly.severity, "status": anomaly.status,
                },
            )
            queued += result["planned"]
        else:
            enqueue_notification(
                db, idempotency_key=event_key, text=message,
                store_id=anomaly.store_id, entity_id=anomaly.entity_id or None,
            )
            queued += 1
    return queued
