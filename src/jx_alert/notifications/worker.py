"""Persistent Feishu application-bot and legacy-webhook outbox worker."""
import logging
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import or_, select, update

from jx_alert.ai.ad_review_skills import frozen_ad_skills_current
from jx_alert.config import get_settings
from jx_alert.notifications.business_daily import select_business_stores
from jx_alert.notifications.feishu import FeishuClient
from jx_alert.notifications.routing import (
    _load_routing, _routed_open_ids, _route_chat_id, _feishu_group_config,
    _database_entity_open_ids as _routing_database_ids,
)
from jx_alert.notifications.outbox import (
    CLAIMABLE, DELIVERY_UNKNOWN, PRE_SEND_RETRY, RESERVING, NotificationOutbox,
)
from jx_alert.notifications.feishu_app import FeishuTokenRefused
from jx_alert.notifications.hourly_ads import BEIJING, CLOSES, OPENS, delivery_window, recheck_hourly_delivery
from jx_alert.notifications.recipient_policy import ROLE, advertising_group_delivery
from jx_alert.notifications.quiet_policy import QUIET_REASON, quiet_blocks, quiet_delivery, original_question_reply
from jx_alert.notifications.store_policy import paused_delivery
from jx_alert.notifications.destination_policy import superseded_group_delivery
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import Campaign, NotificationLog, Store

logger = logging.getLogger(__name__)


def _scheduled_window(key, payload, now, *, hourly_mode=False, created_at=None):
    """Apply Beijing delivery windows to queued proactive notices."""
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    if original_question_reply(key, payload):
        return "READY", None
    current = now.astimezone(BEIJING)
    if not key.startswith("scheduled-daily:"):
        if payload.get('material_ad_delivery_window') is True:
            payload = {**payload, 'hourly_ad_monitor': True}
        if hourly_mode and payload.get('material_ad_alert') is True and not payload.get('hourly_ad_monitor'):
            # Old rows have no hourly evidence or nightly grouping context.
            # Keep them within the daylight window in which they were queued.
            instant = created_at if created_at is not None else now
            created = (instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant).astimezone(BEIJING)
            if (created.date() != current.date() or not OPENS <= created.time() < CLOSES
                    or not OPENS <= current.time() < CLOSES):
                return "EXPIRED", None
        return delivery_window(payload, now)
    try:
        day = date.fromisoformat(payload["scheduled_delivery_date"])
        if payload.get("scheduled_timezone") != "Asia/Shanghai":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return "EXPIRED", None
    focused = key.startswith("scheduled-daily:morning:") or payload.get("morning_focus") is True
    if day < current.date() or (focused and day == current.date() and current.time() >= CLOSES):
        return "EXPIRED", None
    try:
        due = datetime.fromisoformat(payload["scheduled_not_before"]) if payload.get("scheduled_not_before") else (
            datetime.combine(day, time(9), BEIJING))
        if due.tzinfo is None or due.astimezone(BEIJING).date() != day:
            raise ValueError
    except (TypeError, ValueError):
        return "EXPIRED", None
    if current < due:
        return "WAITING", due.astimezone(timezone.utc).replace(tzinfo=None)
    return "READY", None


def _database_entity_open_ids(store_id, entity_id):
    return _routing_database_ids(store_id, entity_id, session_factory=SessionLocal)


def _prior_morning_page(db, payload, row_id):
    batch = payload.get('morning_batch')
    if not batch:
        return False
    return db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.id < row_id,
        NotificationOutbox.idempotency_key.startswith(batch),
        NotificationOutbox.status.in_(RESERVING)).limit(1)) is not None


def _proactive_delivery_reason(db, settings, key, payload, now):
    """Recheck queued automatic work against present store/campaign state."""
    if original_question_reply(key, payload):
        return None
    ids = set(map(str, payload.get('store_ids') or []))
    if payload.get('store_id'):
        ids.add(str(payload['store_id']))
    strict = (key.startswith('scheduled-daily:') or payload.get('material_ad_alert') is True
              or quiet_delivery(settings))
    if ids:
        available = {s.sid for s in db.scalars(select(Store).where(Store.sid.in_(ids), Store.status == 1))}
        # Legacy generic webhook rows without warehouse store records retain
        # their old transport behavior outside the enabled quiet policy.
        known = set(db.scalars(select(Store.sid).where(Store.sid.in_(ids))))
        if known - available or strict and ids - available:
            return '店铺未授权或同步不可用，旧消息保留记录'
    elif strict:
        return '主动消息缺少可核验店铺范围，旧消息保留记录'
    if key.startswith('scheduled-daily:') or payload.get('material_ad_alert') is not True:
        return None
    if len(ids) != 1:
        return '广告告警缺少唯一店铺，旧消息保留记录'
    context = payload.get('routing_context') or {}
    metrics = context.get('metrics') or {}
    campaign_id = payload.get('ad_campaign_id') or metrics.get('campaign_id') or payload.get('entity_id')
    kind = payload.get('ad_campaign_type') or metrics.get('campaign_type')
    if not kind:
        match = re.search(r'(?:campaign_|:campaign:)(sp|sb|sd)$', context.get('scope') or '')
        kind = match.group(1) if match else None
    if not campaign_id or kind is not None and kind not in {'sp', 'sb', 'sd'}:
        return '广告告警缺少可核验活动，旧消息保留记录'
    campaigns = list(db.scalars(select(Campaign).where(Campaign.store_id == next(iter(ids)),
        Campaign.campaign_id == str(campaign_id), Campaign.campaign_type == kind if kind else True)))
    if len(campaigns) != 1:
        return '广告活动身份不唯一或不可用，旧消息保留记录'
    campaign = campaigns[0]
    observed = campaign.updated_at
    if observed is not None and observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
    if ((campaign.state or '').casefold() != 'enabled' or observed is None or observed > now
            or now - observed > timedelta(minutes=90)):
        return '广告活动已停用或状态未实时确认，旧告警保留记录'
    return None


def _business_card_for_delivery(db, settings, key, payload, created_at, now):
    store_ids = payload.get('store_ids') or []
    allowed = set()
    for sid in store_ids:
        scope = {**payload, 'store_ids': [sid], 'store_id': sid}
        if (not paused_delivery(settings, scope, created_at)
                and _proactive_delivery_reason(db, settings, key, scope, now) is None):
            allowed.add(sid)
    return select_business_stores(payload['card'], payload['business_sections'], store_ids, allowed)


class _LostOutboxClaim(RuntimeError):
    """The worker no longer owns a send; it must not call Feishu."""


class _SparkXScopeChanged(RuntimeError):
    """The page is no longer permitted; no message POST has been attempted."""


class _UnfencedResponse(RuntimeError):
    """A transport returned without its write-ahead boundary; do not resubmit."""


_SPARKX_BLOCK_REASON = "SparkX 页面已过期或投递范围已变更，未发起消息 POST，保留记录不补发"
_UNKNOWN_REASON = "可能已提交飞书，未确认送达；禁止自动重发，请人工核对原会话"
_LEGACY_REASON = "旧版尝试没有可核验的提交阶段；禁止自动重发，请人工核对原会话"


def _channel(payload):
    return "feishu_app" if payload.get("transport") == "app" else "feishu_webhook"


def _hold_unknown(db, row, reason):
    changed = db.execute(update(NotificationOutbox).where(
        NotificationOutbox.id == row.id, NotificationOutbox.status == row.status,
        NotificationOutbox.attempts == row.attempts, NotificationOutbox.claimed_at == row.claimed_at,
    ).values(status=DELIVERY_UNKNOWN, claimed_at=None, last_error=reason))
    if changed.rowcount == 1:
        db.add(NotificationLog(dedupe_key=row.idempotency_key, channel=_channel(row.payload),
                               status=DELIVERY_UNKNOWN, error=reason))
        return 1
    return 0


def _record_submission(row_id, claimed_at, attempts, lease_seconds, chat_id, payload, evidence, sparkx_scope=None):
    instant = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh = instant.replace(microsecond=0)
    # A future adapter must not accidentally persist headers or a secret URL.
    intent = {key: evidence[key] for key in ("operation", "body_sha256", "provider_uuid", "endpoint_sha256")
              if key in evidence}
    intent.update(attempt=attempts, started_at_utc=fresh.isoformat() + "Z", destination=chat_id)
    route = payload.get("delivery") or {}
    if route.get("receive_id_type"):
        intent["destination_type"] = route["receive_id_type"]
    if route.get("reply_to_message_id"):
        intent["reply_to_message_id"] = route["reply_to_message_id"]
    with SessionLocal() as db:
        # Token acquisition / earlier jobs can outlive the initial scope or freshness check.
        # Resolve the original binding again at the write-ahead boundary, not after POST.
        if sparkx_scope is not None:
            from jx_alert.sparkx_feishu import delivery_blocked
            key, created_at = sparkx_scope
            if delivery_blocked(db, get_settings(), key, payload, created_at, instant):
                raise _SparkXScopeChanged
        changed = db.execute(update(NotificationOutbox).where(
            NotificationOutbox.id == row_id,
            NotificationOutbox.status == "PREPARING",
            NotificationOutbox.claimed_at == claimed_at,
            NotificationOutbox.attempts == attempts,
            NotificationOutbox.claimed_at >= fresh - timedelta(seconds=lease_seconds),
        ).values(status="SUBMITTING", claimed_at=fresh, chat_id=chat_id,
                 payload={**payload, "delivery_intent": intent}))
        if changed.rowcount != 1:
            raise _LostOutboxClaim
        db.commit()  # Absolutely no message POST until this succeeds.
    return fresh, intent


def process_outbox_batch(batch_size: int = 20, max_attempts: int = 8,
                         outbox_ids: list[int] | None = None) -> dict:
    if outbox_ids is not None and any(type(x) is not int or x <= 0 for x in outbox_ids):
        raise ValueError("outbox_ids must contain positive integers")
    settings = get_settings()
    if settings.app_dry_run or not settings.feishu_enabled:
        return {"sent": 0, "skipped": True, "reason": "dry-run or Feishu disabled"}
    transport = getattr(settings, "feishu_transport", "webhook")
    hourly_mode = getattr(settings, "hourly_ad_monitor_enabled", False)
    routing = {} if transport == "app" else _load_routing(settings.feishu_routing_file)
    if transport == "webhook" and not routing.get("groups") and not settings.feishu_webhook_url:
        raise RuntimeError("FEISHU_WEBHOOK_URL is required when FEISHU_ENABLED=true")
    default_ids = [] if transport == "app" else settings.feishu_at_ids()
    default_chat_id = routing.get("default_chat_id", "default")
    client_cache = {}
    # Existing MariaDB DATETIME columns have second precision. Use the same
    # precision for the claim and its CAS predicate; attempts fence same-second claims.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    batch_claim_time = now.replace(microsecond=0)
    # Cover the complete claimed batch, including token acquisition/refresh.
    lease_seconds = max(300, settings.feishu_timeout * 20 * 5 + 60)
    stale_before = now - timedelta(seconds=lease_seconds)

    # A PREPARING claim has not crossed the write-ahead send fence. Legacy
    # SENDING or new SUBMITTING might already have reached the remote chat.
    selection = NotificationOutbox.id.in_(outbox_ids) if outbox_ids is not None else True
    stale = or_(NotificationOutbox.claimed_at.is_(None), NotificationOutbox.claimed_at < stale_before)
    unknown = 0
    with SessionLocal() as db:
        for row in db.scalars(select(NotificationOutbox).where(selection, stale,
                NotificationOutbox.status.in_(("SENDING", "SUBMITTING")))
                .order_by(NotificationOutbox.id).limit(100).with_for_update(skip_locked=True)):
            unknown += _hold_unknown(db, row, _LEGACY_REASON if row.status == "SENDING" else _UNKNOWN_REASON)
        db.execute(update(NotificationOutbox).where(selection, stale,
            NotificationOutbox.status == "PREPARING").values(
                status=PRE_SEND_RETRY, claimed_at=None, next_attempt_at=now))
        db.commit()

        capacity = max(1, min(batch_size, 5))
        jobs = []
        while len(jobs) < capacity:
            rows = db.scalars(
            select(NotificationOutbox)
            .where(selection)
            .where(
                NotificationOutbox.status.in_(CLAIMABLE),
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
            .limit(capacity - len(jobs))
            .with_for_update(skip_locked=True)
            ).all()
            if not rows:
                break
            for row in rows:
                if row.payload.get("delivery_intent") is not None or (
                        row.attempts > 0 and row.status != PRE_SEND_RETRY):
                    # Old RETRY rows did not distinguish a timeout from an
                    # error before send; even an expired row needs truthful history.
                    reason = _UNKNOWN_REASON if row.payload.get("delivery_intent") else _LEGACY_REASON
                    unknown += _hold_unknown(db, row, reason)
                    continue
                window, due = _scheduled_window(row.idempotency_key, row.payload, now,
                                                hourly_mode=hourly_mode, created_at=row.created_at)
                if window == "EXPIRED":
                    row.status, row.last_error = "EXPIRED", "已过通知时段，保留记录"
                    continue
                if window == "WAITING":
                    row.next_attempt_at = due
                    continue
                attempts = row.attempts + 1
                claimed = db.execute(update(NotificationOutbox).where(
                    NotificationOutbox.id == row.id, NotificationOutbox.status == row.status,
                    NotificationOutbox.attempts == row.attempts, NotificationOutbox.next_attempt_at <= now,
                ).values(status="PREPARING", claimed_at=batch_claim_time, attempts=attempts))
                if not claimed.rowcount:
                    continue
                jobs.append((row.id, row.idempotency_key, dict(row.payload), attempts, row.chat_id, row.created_at))
            db.flush()
        db.commit()

    sent = 0
    failed = 0
    for row_id, dedupe_key, payload, attempts, existing_chat_id, created_at in jobs:
        claim_time = batch_claim_time
        intent = None

        destination = payload.get("delivery")
        sparkx_page = (dedupe_key.startswith("sparkx-page:") or isinstance(destination, dict)
                       and destination.get("role") == "sparkx_performance_page")

        def fence_send(evidence):
            nonlocal claim_time, intent
            claim_time, intent = _record_submission(row_id, claim_time, attempts, lease_seconds,
                                                     chat_id, payload, evidence,
                                                     (dedupe_key, created_at) if sparkx_page else None)
            if sparkx_page:
                from jx_alert.sparkx_feishu import page_fresh
                # Scope reads, a DB lock or the intent COMMIT itself may cross the expiry.
                # No more policy/database I/O between this check and return to the POST.
                if not page_fresh(created_at, datetime.now(timezone.utc).replace(tzinfo=None)):
                    intent = {**intent, "aborted_before_post": True}
                    raise _SparkXScopeChanged

        channel = _channel(payload)
        message_id = None
        hourly_delivered_items = None
        chat_id = (
            default_chat_id
            if existing_chat_id in (None, "", "default")
            else existing_chat_id
        )
        try:
            # An earlier page may be retrying. A later page must never overtake it.
            with SessionLocal() as db:
                if _prior_morning_page(db, payload, row_id):
                    db.execute(update(NotificationOutbox).where(
                        NotificationOutbox.id == row_id, NotificationOutbox.status == "PREPARING",
                        NotificationOutbox.claimed_at == claim_time, NotificationOutbox.attempts == attempts,
                    ).values(status=PRE_SEND_RETRY, claimed_at=None, attempts=attempts - 1,
                             next_attempt_at=now + timedelta(seconds=30)))
                    db.commit()
                    continue
            skip_reason = QUIET_REASON if quiet_blocks(settings, dedupe_key, payload) else None
            if (skip_reason is None and dedupe_key.startswith('scheduled-daily:')
                    and payload.get('morning_message_kind') == 'ad_exception'
                    and not frozen_ad_skills_current(settings, payload.get('ad_skill_checks'))):
                skip_reason = '广告分析规则已变更或无法核验，日报广告页保留记录'
            if skip_reason is None and sparkx_page:
                from jx_alert.sparkx_feishu import delivery_blocked
                with SessionLocal() as db:
                    if delivery_blocked(db, settings, dedupe_key, payload, created_at, now):
                        skip_reason = _SPARKX_BLOCK_REASON
            hourly_batch = isinstance(payload.get('hourly_items'), list)
            business_batch = (dedupe_key.startswith('scheduled-daily:')
                and payload.get('morning_message_kind') == 'business_daily' and 'business_sections' in payload)
            if skip_reason is None and payload.get('hourly_ad_monitor') is True and not hourly_batch:
                skip_reason = '旧版即时告警保留记录'
            if skip_reason is None and payload.get("hourly_ad_monitor") is True and not payload.get('hourly_overnight') and (
                    now - created_at.replace(tzinfo=None) > timedelta(minutes=90)):
                skip_reason = "即时告警已过有效期，保留记录"
            if skip_reason is None and payload.get("transport") == "app":
                destination = payload.get("delivery") or {}
                if destination.get("role") in {"group_question", "private_question"}:
                    from jx_alert.bot_alert_history import unscoped_frozen_history
                    from jx_alert.bot_service import validate_question_delivery
                    if unscoped_frozen_history(payload):
                        skip_reason = "历史回复缺少可核验的原群或店铺范围，旧回复保留记录"
                    elif not validate_question_delivery(settings, destination):
                        skip_reason = "原会话、店铺范围或通知群权限已变更，旧回复保留记录"
                if skip_reason is None and superseded_group_delivery(settings, payload, created_at):
                    skip_reason = "通知群已更换，旧消息保留记录，不迁移或补发"
                if (skip_reason is None and getattr(settings, 'hourly_ad_monitor_enabled', False)
                        and destination.get('receive_id_type') == 'open_id' and destination.get('role') != ROLE
                        and not original_question_reply(dedupe_key, payload)):
                    skip_reason = "主动提醒使用通知群，旧私聊保留记录"
                if skip_reason is None and payload.get('ad_group_owners') is True:
                    store_ids = payload.get('store_ids') or [payload.get('store_id')]
                    fresh = advertising_group_delivery(settings, _load_routing(settings.feishu_routing_file),
                                                        store_ids, role=destination['role'])
                    if (fresh['receive_id'] != destination.get('receive_id')
                            or destination.get('receive_id_type') != 'chat_id'):
                        skip_reason = "通知群已变更，旧消息保留记录"
                    else:
                        payload['delivery'] = fresh
            if skip_reason is None and not (hourly_batch or business_batch) and paused_delivery(settings, payload, created_at):
                skip_reason = "店铺通知已停用，旧消息保留记录"
            if skip_reason is None:
                with SessionLocal() as db:
                    if hourly_batch:
                        checked = recheck_hourly_delivery(db, settings, payload, now.replace(tzinfo=timezone.utc))
                        if checked is None:
                            skip_reason = '当前接收权限或活动状态不满足，旧告警保留记录'
                        else:
                            payload['card'], hourly_delivered_items = checked
                    elif business_batch:
                        value = _business_card_for_delivery(db, settings, dedupe_key, payload, created_at, now)
                        if value is None:
                            skip_reason = '店铺未授权、通知已停用或经营日报无法核验，旧消息保留记录'
                        else:
                            payload['card'] = value
                    else:
                        skip_reason = _proactive_delivery_reason(db, settings, dedupe_key, payload, now)
            if skip_reason is None and payload.get('morning_message_kind') == 'owner_daily':
                from jx_alert.notifications.owner_daily import owner_daily_delivery_card

                value = owner_daily_delivery_card(settings, dedupe_key, payload)
                if value is None:
                    skip_reason = '负责人日报已停用或当前店铺、通知群范围无法核验，旧消息保留记录'
                else:
                    payload['card'] = value
            if skip_reason is None and (payload.get('delivery') or {}).get('role') == 'ad_alert_subscriber':
                from jx_alert.notifications.recipient_policy import subscriber_delivery_blocked
                if subscriber_delivery_blocked(settings, payload):
                    skip_reason = '广告异常私聊订阅已变更，旧消息保留记录'
            if skip_reason is not None:
                with SessionLocal() as db:
                    skipped = db.execute(update(NotificationOutbox).where(
                        NotificationOutbox.id == row_id, NotificationOutbox.status == "PREPARING",
                        NotificationOutbox.claimed_at == claim_time, NotificationOutbox.attempts == attempts,
                    ).values(status="SKIPPED", claimed_at=None, last_error=skip_reason))
                    if skipped.rowcount == 1:
                        db.add(NotificationLog(dedupe_key=dedupe_key, channel=channel, status="SKIPPED",
                                               error=skip_reason))
                    db.commit()
                continue
            window, due = _scheduled_window(dedupe_key, payload, datetime.now(timezone.utc),
                                            hourly_mode=hourly_mode, created_at=created_at)
            if window != "READY":
                with SessionLocal() as db:
                    values = (dict(status=PRE_SEND_RETRY, next_attempt_at=due, attempts=attempts - 1)
                              if window == "WAITING" else dict(status="EXPIRED", last_error="已过通知时段"))
                    db.execute(update(NotificationOutbox).where(
                        NotificationOutbox.id == row_id, NotificationOutbox.status == "PREPARING",
                        NotificationOutbox.claimed_at == claim_time, NotificationOutbox.attempts == attempts,
                    ).values(claimed_at=None, **values))
                    db.commit()
                continue
            payload_ids = payload.get("open_ids") or []
            if not isinstance(payload_ids, list) or not all(
                isinstance(item, str) and item.startswith("ou_") for item in payload_ids
            ):
                raise ValueError("outbox open_ids must be a list of Feishu Open IDs")
            if payload.get("transport") == "app":
                from jx_alert.notifications.feishu_app import FeishuAppClient
                if "app" not in client_cache:
                    client_cache["app"] = FeishuAppClient.from_settings(settings)
                destination = payload["delivery"]
                chat_id = destination["receive_id"]
                card = payload.get("card")
                if destination.get("reply_to_message_id"):
                    if destination.get("role") not in {"group_question", "private_question"} or card is None:
                        raise ValueError("Only verified question cards can reply to their original message")
                    reply_args = {"idempotency_key": dedupe_key}
                    if destination["role"] == "private_question":
                        reply_args["reply_in_thread"] = False
                    response = client_cache["app"].reply_card(destination["reply_to_message_id"], card,
                                                              before_submit=fence_send, **reply_args)
                else:
                    send_message = client_cache["app"].send_card if card is not None else client_cache["app"].send_text
                    response = send_message(
                        card if card is not None else str(payload.get("text") or ""),
                        receive_id=chat_id,
                        receive_id_type=destination["receive_id_type"],
                        open_ids=destination.get("open_ids", []),
                        idempotency_key=dedupe_key, before_submit=fence_send,
                    )
                message_id = response["message_id"]
            else:
                # Retain compatibility for persisted webhook notifications.
                chat_id, webhook_client, open_ids = _prepare_webhook(
                    settings, routing, client_cache, payload, payload_ids, default_ids, chat_id)
                webhook_client.send_text(str(payload.get("text") or ""), open_ids=open_ids,
                                         before_submit=fence_send)
            if intent is None:
                raise _UnfencedResponse("Feishu transport returned without recording a submission")
            status = "DELIVERED"
            error = None
            delivered_at = datetime.now(timezone.utc).replace(tzinfo=None)
        except _SparkXScopeChanged:
            status, error, delivered_at = "SKIPPED", _SPARKX_BLOCK_REASON, None
        except _LostOutboxClaim:
            logger.info("Feishu send skipped after losing outbox claim id=%s", row_id)
            continue
        except Exception as exc:
            if isinstance(exc, _UnfencedResponse):
                status = DELIVERY_UNKNOWN
            elif intent is not None:
                status = "REJECTED" if isinstance(exc, FeishuTokenRefused) else DELIVERY_UNKNOWN
            else:
                status = "DEAD" if attempts >= max_attempts else PRE_SEND_RETRY
            # Persist only the type; provider responses may echo credentials or content.
            error = f"Feishu delivery failed: {type(exc).__name__}"
            code = re.search(r"\bcode=(-?\d+)\b", str(exc))
            if code:
                error += f" code={code[1]}"
            if status == DELIVERY_UNKNOWN:
                error = f"{_UNKNOWN_REASON} ({error})"
            delivered_at = None
            logger.warning("Feishu outbox id=%s status=%s (%s)", row_id, status, type(exc).__name__)

        with SessionLocal() as db:
            values = dict(chat_id=chat_id, status=status, last_error=error,
                          claimed_at=None, delivered_at=delivered_at)
            if status == "SKIPPED" and intent is not None and intent.get("aborted_before_post") is True:
                values["payload"] = {**payload, "delivery_intent": intent}
            if status == "DELIVERED" and hourly_delivered_items is not None:
                values["payload"] = {**payload, "delivery_intent": intent, "hourly_delivered_items": hourly_delivered_items}
            if status == PRE_SEND_RETRY:
                delay = min(30 * (2 ** max(attempts - 1, 0)), 3600)
                values["next_attempt_at"] = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)
            result = db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == row_id,
                NotificationOutbox.status == ("SUBMITTING" if intent is not None else "PREPARING"),
                NotificationOutbox.claimed_at == claim_time,
                NotificationOutbox.attempts == attempts,
            ).values(**values))
            # An obsolete attempt must never overwrite a newer worker's result.
            attempt_status = status if result.rowcount == 1 else (
                "STALE_UNKNOWN" if status == DELIVERY_UNKNOWN else f"STALE_{status}")
            db.add(NotificationLog(
                dedupe_key=dedupe_key,
                channel=channel,
                status=attempt_status,
                message_id=message_id,
                error=error,
            ))
            db.commit()
            if result.rowcount == 1:
                sent += int(status == "DELIVERED")
                unknown += int(status == DELIVERY_UNKNOWN)
                failed += int(status not in ("DELIVERED", DELIVERY_UNKNOWN, "SKIPPED"))

    return {"sent": sent, "failed": failed, "unknown": unknown, "claimed": len(jobs), "skipped": False}


def _prepare_webhook(settings, routing, client_cache, payload, payload_ids, default_ids, chat_id):
    if getattr(settings, "feishu_transport", "webhook") == "app":
        routing = _load_routing(settings.feishu_routing_file)
    if not payload.get("fixed_route"):
        chat_id = _route_chat_id(
            routing,
            payload.get("store_id"),
            payload.get("entity_id"),
            fallback=chat_id,
        )
    group = _feishu_group_config(routing, chat_id, settings)
    if chat_id not in client_cache:
        client_cache[chat_id] = FeishuClient(
            webhook_url=group["webhook_url"],
            sign_secret=group["sign_secret"],
            keyword=group["keyword"],
            timeout=settings.feishu_timeout,
        )
    open_ids = list(dict.fromkeys(
        default_ids
        + group["admin_open_ids"]
        + _routed_open_ids(routing, payload.get("store_id"), payload.get("entity_id"))
        + _database_entity_open_ids(payload.get("store_id"), payload.get("entity_id"))
        + payload_ids
    ))
    return chat_id, client_cache[chat_id], open_ids
