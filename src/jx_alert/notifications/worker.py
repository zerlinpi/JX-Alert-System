"""At-least-once outbox worker for the Feishu custom-bot webhook."""
from datetime import datetime, timedelta
from sqlalchemy import or_, select, update

from jx_alert.config import get_settings
from jx_alert.notifications.feishu import FeishuClient
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import NotificationLog


def process_outbox_batch(batch_size: int = 20, max_attempts: int = 8) -> dict:
    settings = get_settings()
    if settings.app_dry_run or not settings.feishu_enabled:
        return {"sent": 0, "skipped": True, "reason": "dry-run or Feishu disabled"}
    if not settings.feishu_webhook_url:
        raise RuntimeError("FEISHU_WEBHOOK_URL is required when FEISHU_ENABLED=true")

    now = datetime.utcnow()
    stale_before = now - timedelta(minutes=5)
    client = FeishuClient(
        webhook_url=settings.feishu_webhook_url,
        sign_secret=settings.feishu_sign_secret,
        keyword=settings.feishu_keyword,
        timeout=settings.feishu_timeout,
    )

    # Recover claims left behind by a crashed worker. This is at-least-once delivery:
    # a crash after Feishu accepts a message but before DB commit can cause a duplicate.
    with SessionLocal() as db:
        db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.status == "SENDING",
                NotificationOutbox.claimed_at < stale_before,
            )
            .values(status="RETRY", claimed_at=None, next_attempt_at=now)
        )
        db.commit()

        rows = db.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.status.in_(["PENDING", "RETRY"]),
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
            .limit(max(1, min(batch_size, 100)))
            .with_for_update(skip_locked=True)
        ).all()
        jobs = []
        for row in rows:
            row.status = "SENDING"
            row.claimed_at = now
            row.attempts += 1
            jobs.append((row.id, row.idempotency_key, dict(row.payload), row.attempts))
        db.commit()

    sent = 0
    failed = 0
    default_ids = settings.feishu_at_ids()
    for row_id, dedupe_key, payload, attempts in jobs:
        open_ids = list(dict.fromkeys(default_ids + list(payload.get("open_ids") or [])))
        try:
            result = client.send_text(str(payload.get("text") or ""), open_ids=open_ids)
            status = "DELIVERED"
            error = None
            delivered_at = datetime.utcnow()
            sent += 1
        except Exception as exc:  # store a sanitized exception string; never store webhook URL
            status = "DEAD" if attempts >= max_attempts else "RETRY"
            error = f"{type(exc).__name__}: {str(exc)[:1000]}"
            delivered_at = None
            failed += 1

        with SessionLocal() as db:
            row = db.get(NotificationOutbox, row_id)
            if row:
                row.status = status
                row.last_error = error
                row.claimed_at = None
                row.delivered_at = delivered_at
                if status == "RETRY":
                    delay = min(30 * (2 ** max(attempts - 1, 0)), 3600)
                    row.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay)
            db.add(NotificationLog(
                dedupe_key=dedupe_key,
                channel="feishu_webhook",
                status=status,
                message_id=None,
                error=error,
            ))
            db.commit()

    return {"sent": sent, "failed": failed, "claimed": len(jobs), "skipped": False}
