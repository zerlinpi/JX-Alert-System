"""Persistent Feishu outbox worker (enterprise application transport only).

PENDING/RETRY/PRE_SEND_RETRY → PREPARING (claimed, nothing sent) → SUBMITTING
(write-ahead intent committed; the POST may have happened) → DELIVERED,
DELIVERY_UNKNOWN, REJECTED, DEAD, SKIPPED or EXPIRED. A row that may have
reached Feishu is never resent automatically. Rows of a kind this release does
not send (for example queued by an earlier release) are skipped and kept.

The Excel copy of a lane message (transport ``app_file``) follows the same rules:
its saved file is checked against the queued SHA-256, uploaded to Feishu (an upload
sends nothing to anyone), then sent as one file message behind the same fence.
Releases before Excel copies do not know this transport and skip such rows.
"""
from datetime import datetime, timedelta, timezone
import logging
import re

from sqlalchemy import or_, select, update

from jx_alert.config import get_settings
from jx_alert.notifications.feishu_app import FeishuRejected, FeishuTokenRefused, FeishuUploadRejected
from jx_alert.notifications.outbox import (
    CLAIMABLE, DELIVERY_UNKNOWN, PRE_SEND_RETRY, QUESTION_REPLY_PREFIX, NotificationOutbox,
)
from jx_alert.reports import archive, lanes
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import NotificationLog

logger = logging.getLogger(__name__)
CHANNEL = "feishu_app"
LEGACY_REASON = "旧版本的通知类型已停用，保留记录不发送"
UNKNOWN_REASON = "可能已提交飞书，未确认送达；禁止自动重发，请人工核对原会话"
LEGACY_ATTEMPT_REASON = "旧版尝试没有可核验的提交阶段；禁止自动重发，请人工核对原会话"
REPLY_ROLES = frozenset({"group_question", "private_question"})
# Feishu's documented refusals of a message (im/v1/messages, 错误码): the reason shown to the owner.
REJECTIONS = {
    230002: "机器人不在该群", 230013: "接收人不在飞书应用的可用范围内（或已离职），请在开发者后台把此人加入应用可用范围",
    230018: "群设置不允许机器人发言", 230020: "触发飞书频率限制", 230025: "消息内容超出飞书长度限制",
    230029: "接收人已离职", 230035: "没有发送权限（群禁言或机器人被屏蔽）", 230053: "接收人已设置不接收机器人消息",
    232009: "群已解散",
}
# Feishu's refusals of a file upload (im/v1/files, 错误码 and 通用错误码): nothing was sent.
UPLOAD_REJECTIONS = {
    99991672: "飞书应用缺少上传文件的权限（im:resource），请在开发者后台开通并发布新版本",
    234006: "文件超过飞书大小上限", 234007: "飞书应用没有启用机器人能力", 234010: "文件为空",
}
# Rate limits are temporary: those uploads are retried like any other failure before sending.
RATE_LIMITED = frozenset({99991400, 230020})
FILE_KINDS = frozenset({"lane_file"})
LANE_KINDS = frozenset({"lane", "lane_file"})


class _LostClaim(RuntimeError):
    """The worker no longer owns this row; it must not call Feishu."""


class _Blocked(RuntimeError):
    """The send is no longer wanted; no POST has been attempted."""


class _Unfenced(RuntimeError):
    """A transport returned without its write-ahead boundary; do not resubmit."""


def _clock():
    """Aware UTC now; tests replace this single source of time."""
    return datetime.now(timezone.utc)


def _utcnow():
    return _clock().replace(tzinfo=None)


def _kind(key, payload):
    delivery = payload.get("delivery") if isinstance(payload, dict) else None
    if not isinstance(delivery, dict):
        return None
    if payload.get("transport") == lanes.FILE_TRANSPORT:
        # The Excel copy of a lane message; releases before it skip these rows (other transport).
        return "lane_file" if lanes.is_lane(key, payload) and isinstance(payload.get("file"), dict) else None
    if payload.get("transport") != "app":
        return None
    if lanes.is_lane(key, payload):
        return "lane"
    if key.startswith(QUESTION_REPLY_PREFIX) and delivery.get("role") in REPLY_ROLES:
        return "reply"
    return None


def _window(kind, key, payload, now):
    return lanes.delivery_window(key, payload, now) if kind in LANE_KINDS else ("READY", None)


def _blocked(settings, kind, key, payload, now):
    if kind in LANE_KINDS:
        return lanes.delivery_blocked(settings, key, payload, now)
    from jx_alert.bot_service import validate_question_delivery
    if not validate_question_delivery(settings, payload["delivery"]):
        return "原会话或读取权限已变更，旧回复保留记录"
    return None


def _log(db, key, status, *, error=None, message_id=None):
    db.add(NotificationLog(dedupe_key=key, channel=CHANNEL, status=status, error=error, message_id=message_id))


def _hold_unknown(db, row, reason):
    changed = db.execute(update(NotificationOutbox).where(
        NotificationOutbox.id == row.id, NotificationOutbox.status == row.status,
        NotificationOutbox.attempts == row.attempts, NotificationOutbox.claimed_at == row.claimed_at,
    ).values(status=DELIVERY_UNKNOWN, claimed_at=None, last_error=reason))
    if changed.rowcount == 1:
        _log(db, row.idempotency_key, DELIVERY_UNKNOWN, error=reason)
        return 1
    return 0


def _record_submission(session_factory, settings, job, claimed_at, lease_seconds, evidence):
    """Re-check the policy, then commit the write-ahead intent. No POST before this returns."""
    row_id, key, payload, attempts, kind = job["id"], job["key"], job["payload"], job["attempts"], job["kind"]
    intent = {name: evidence[name] for name in ("operation", "body_sha256", "provider_uuid", "app_identity_sha256")
              if name in evidence}
    route = payload["delivery"]
    intent.update(attempt=attempts, destination=route["receive_id"], destination_type=route.get("receive_id_type"))
    if route.get("reply_to_message_id"):
        intent["reply_to_message_id"] = route["reply_to_message_id"]
    with session_factory() as db:
        row = db.scalar(select(NotificationOutbox).where(
            NotificationOutbox.id == row_id, NotificationOutbox.status == "PREPARING",
            NotificationOutbox.claimed_at == claimed_at, NotificationOutbox.attempts == attempts,
        ).with_for_update())
        if row is None:
            raise _LostClaim
        if row.payload != payload or row.idempotency_key != key:
            raise _Blocked("消息内容已变化，未发起 POST")
        # Token acquisition can outlive the first check; resolve the binding again.
        reason = _blocked(settings, kind, key, payload, _clock())
        if reason:
            raise _Blocked(reason)
        checked = _utcnow().replace(microsecond=0)
        intent["started_at_utc"] = checked.isoformat() + "Z"
        changed = db.execute(update(NotificationOutbox).where(
            NotificationOutbox.id == row_id, NotificationOutbox.status == "PREPARING",
            NotificationOutbox.claimed_at == claimed_at, NotificationOutbox.attempts == attempts,
            NotificationOutbox.claimed_at >= checked - timedelta(seconds=lease_seconds),
        ).values(status="SUBMITTING", claimed_at=checked, payload={**payload, "delivery_intent": intent}))
        if changed.rowcount != 1:
            raise _LostClaim
        db.commit()
    return checked, intent


def _claim(db, selection, capacity, now, claim_time):
    jobs, unknown = [], 0
    while len(jobs) < capacity:
        rows = db.scalars(select(NotificationOutbox).where(
            *selection, NotificationOutbox.status.in_(CLAIMABLE), NotificationOutbox.next_attempt_at <= now,
        ).order_by(NotificationOutbox.created_at, NotificationOutbox.id).limit(capacity - len(jobs))
            .with_for_update(skip_locked=True)).all()
        if not rows:
            break
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("delivery_intent") is not None or (row.attempts > 0 and row.status != PRE_SEND_RETRY):
                reason = UNKNOWN_REASON if payload.get("delivery_intent") else LEGACY_ATTEMPT_REASON
                unknown += _hold_unknown(db, row, reason)
                continue
            kind = _kind(row.idempotency_key, payload)
            if kind is None:
                row.status, row.last_error = "SKIPPED", LEGACY_REASON
                _log(db, row.idempotency_key, "SKIPPED", error=LEGACY_REASON)
                continue
            window, due = _window(kind, row.idempotency_key, payload, now)
            if window == "EXPIRED":
                row.status, row.last_error = "EXPIRED", "已过发送时段，保留记录"
                continue
            if window == "WAITING":
                row.next_attempt_at = due
                continue
            attempts = row.attempts + 1
            claimed = db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == row.id, NotificationOutbox.status == row.status,
                NotificationOutbox.attempts == row.attempts, NotificationOutbox.next_attempt_at <= now,
            ).values(status="PREPARING", claimed_at=claim_time, attempts=attempts))
            if claimed.rowcount:
                jobs.append({"id": row.id, "key": row.idempotency_key, "payload": dict(payload),
                             "attempts": attempts, "kind": kind})
        db.flush()
    return jobs, unknown


def _mentions(settings, kind, payload):
    """Lane group copies mention the still-valid operators (with their stores); nothing else mentions anyone."""
    return lanes.mention_plan(settings, payload) if kind == "lane" else ([], {})


def _attachment(settings, kind, payload):
    """(bytes, file name) of an Excel copy, read before anything is sent; None for other messages."""
    if kind not in FILE_KINDS:
        return None
    try:
        return archive.read(settings, payload["file"]), payload["file"]["name"]
    except (archive.ArchiveError, KeyError, TypeError):
        raise _Blocked("Excel 文件缺失或内容已变化，未发送") from None


def _send(client, payload, key, fence, mentions, labels, attachment=None):
    destination = payload["delivery"]
    if attachment is not None:
        data, name = attachment
        return client.send_file(data, name, receive_id=destination["receive_id"],
                                receive_id_type=destination["receive_id_type"], idempotency_key=key,
                                before_submit=fence)
    card = payload.get("card")
    if destination.get("reply_to_message_id"):
        if destination.get("role") not in REPLY_ROLES or card is None:
            raise ValueError("Only verified question cards can reply to their original message")
        return client.reply_card(destination["reply_to_message_id"], card, idempotency_key=key,
                                 reply_in_thread=destination["role"] != "private_question", before_submit=fence)
    if card is not None:
        return client.send_card(card, receive_id=destination["receive_id"],
                                receive_id_type=destination["receive_id_type"],
                                open_ids=mentions, mention_labels=labels, idempotency_key=key, before_submit=fence)
    return client.send_text(str(payload.get("text") or ""), receive_id=destination["receive_id"],
                            receive_id_type=destination["receive_id_type"], open_ids=mentions,
                            idempotency_key=key, before_submit=fence)


def process_outbox_batch(batch_size=5, max_attempts=8, outbox_ids=None, *, question_replies_only=False,
                         session_factory=None, client_factory=None, settings=None):
    if outbox_ids is not None and any(type(x) is not int or x <= 0 for x in outbox_ids):
        raise ValueError("outbox_ids must contain positive integers")
    settings = settings or get_settings()
    if not settings.delivery_enabled():
        return {"sent": 0, "skipped": True, "reason": "dry-run or Feishu app delivery disabled"}
    session_factory = session_factory or SessionLocal
    now = _utcnow()
    claim_time = now.replace(microsecond=0)  # MariaDB DATETIME has second precision.
    lease_seconds = max(300, settings.feishu_timeout * 20 * 5 + 60)
    stale_before = now - timedelta(seconds=lease_seconds)
    capacity = max(1, min(batch_size, 5))
    selection = [NotificationOutbox.id.in_(outbox_ids)] if outbox_ids is not None else []
    if question_replies_only:
        selection.append(NotificationOutbox.idempotency_key.startswith(QUESTION_REPLY_PREFIX))
    stale = or_(NotificationOutbox.claimed_at.is_(None), NotificationOutbox.claimed_at < stale_before)
    unknown = 0
    with session_factory() as db:
        for row in db.scalars(select(NotificationOutbox).where(
                *selection, stale, NotificationOutbox.status.in_(("SENDING", "SUBMITTING")))
                .order_by(NotificationOutbox.id).limit(100).with_for_update(skip_locked=True)):
            unknown += _hold_unknown(db, row, LEGACY_ATTEMPT_REASON if row.status == "SENDING" else UNKNOWN_REASON)
        db.execute(update(NotificationOutbox).where(*selection, stale, NotificationOutbox.status == "PREPARING")
                   .values(status=PRE_SEND_RETRY, claimed_at=None, next_attempt_at=now))
        db.commit()
        jobs, held = _claim(db, selection, capacity, now, claim_time)
        unknown += held
        db.commit()

    client = None
    sent = failed = 0
    for job in jobs:
        claimed_at, intent, message_id = claim_time, None, None
        key, payload = job["key"], job["payload"]

        def fence(evidence, job=job):
            nonlocal claimed_at, intent
            claimed_at, intent = _record_submission(session_factory, settings, job, claimed_at, lease_seconds, evidence)
            # The intent COMMIT may cross the window boundary; no more I/O before the POST.
            if job["kind"] in LANE_KINDS and lanes.delivery_window(key, payload, _clock())[0] != "READY":
                intent = {**intent, "aborted_before_post": True}
                raise _Blocked("已过发送时段，保留记录不补发")

        try:
            reason = _blocked(settings, job["kind"], key, payload, _clock())
            if reason:
                raise _Blocked(reason)
            mentions, labels = _mentions(settings, job["kind"], payload)
            attachment = _attachment(settings, job["kind"], payload)
            if client is None:
                from jx_alert.notifications.feishu_app import FeishuAppClient
                client = (client_factory or FeishuAppClient.from_settings)(settings)
            response = _send(client, payload, key, fence, mentions, labels, attachment)
            if intent is None:
                raise _Unfenced("Feishu transport returned without recording a submission")
            message_id = response["message_id"]
            status, error, delivered_at = "DELIVERED", None, _utcnow()
        except _Blocked as exc:
            status, error, delivered_at = "SKIPPED", str(exc), None
        except _LostClaim:
            logger.info("Feishu send skipped after losing outbox claim id=%s", job["id"])
            continue
        except Exception as exc:  # noqa: BLE001 - classified by phase, never by provider text.
            if isinstance(exc, _Unfenced):
                status = DELIVERY_UNKNOWN
            elif intent is not None:
                # Feishu's own error answer proves nothing was created; anything else may have been.
                status = "REJECTED" if isinstance(exc, (FeishuTokenRefused, FeishuRejected)) else DELIVERY_UNKNOWN
            elif isinstance(exc, FeishuUploadRejected) and exc.code not in RATE_LIMITED:
                status = "REJECTED"  # the file was refused before any message: retrying would not help
            else:
                status = "DEAD" if job["attempts"] >= max_attempts else PRE_SEND_RETRY
            error = f"Feishu delivery failed: {type(exc).__name__}"
            code = re.search(r"\bcode=(-?\d+)\b", str(exc))
            if code:
                error += f" code={code[1]}"
            if status == DELIVERY_UNKNOWN:
                error = f"{UNKNOWN_REASON} ({error})"
            elif status == "REJECTED" and isinstance(exc, FeishuUploadRejected):
                error = (f"飞书拒绝接收 Excel 文件，未发送（code={exc.code}"
                         + (f"：{UPLOAD_REJECTIONS[exc.code]}）" if exc.code in UPLOAD_REJECTIONS else "）"))
            elif status == "REJECTED" and isinstance(exc, FeishuRejected):
                error = (f"飞书拒绝，未送达（code={exc.code}"
                         + (f"：{REJECTIONS[exc.code]}）" if exc.code in REJECTIONS else "）"))
            delivered_at = None
            logger.warning("Feishu outbox id=%s status=%s (%s)", job["id"], status, type(exc).__name__)
        with session_factory() as db:
            values = dict(status=status, last_error=error, claimed_at=None, delivered_at=delivered_at)
            if status == "SKIPPED" and intent is not None:
                values["payload"] = {**payload, "delivery_intent": intent}
            if status == PRE_SEND_RETRY:
                delay = min(30 * (2 ** max(job["attempts"] - 1, 0)), 3600)
                values["next_attempt_at"] = _utcnow() + timedelta(seconds=delay)
            result = db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == job["id"],
                NotificationOutbox.status == ("SUBMITTING" if intent is not None else "PREPARING"),
                NotificationOutbox.claimed_at == claimed_at, NotificationOutbox.attempts == job["attempts"],
            ).values(**values))
            # An obsolete attempt must never overwrite a newer worker's result.
            stale_status = "STALE_UNKNOWN" if status == DELIVERY_UNKNOWN else f"STALE_{status}"
            _log(db, key, status if result.rowcount == 1 else stale_status, error=error, message_id=message_id)
            db.commit()
            if result.rowcount == 1:
                sent += status == "DELIVERED"
                unknown += status == DELIVERY_UNKNOWN
                failed += status not in ("DELIVERED", DELIVERY_UNKNOWN, "SKIPPED")
    return {"sent": sent, "failed": failed, "unknown": unknown, "claimed": len(jobs), "skipped": False}
