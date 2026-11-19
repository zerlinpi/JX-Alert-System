"""Single opt-in private lead for evidence-backed advertising alerts."""
import json
from pathlib import Path
import re
import hashlib
from datetime import timezone


SECTION = 'ad_alert_subscribers'
ROLE = 'ad_alert_subscriber'
AD_SCOPES = frozenset({'campaign_sp', 'campaign_sb', 'campaign_sd', 'product_ad_sp', 'product_ad_sd'})


def advertising_scope(scope, store_id):
    return scope in AD_SCOPES or scope in {
        f'{store_id}:{family}:{kind}' for family in ('campaign', 'product_ad') for kind in ('sp', 'sb', 'sd')}


def advertising_group_delivery(settings, routing, store_ids, *, role='group'):
    """Mention only the mapped store owners in the verified report group."""
    from jx_alert.notifications.routing import report_group_delivery, report_group_owner_mentions

    destination = report_group_delivery(settings, routing, role=role)
    destination['open_ids'] = list(dict.fromkeys(uid for sid in store_ids
        for uid in report_group_owner_mentions(routing, destination, str(sid))))
    return destination


def enqueue_advertising_copies(db, settings, *, key, text, card, destination,
                               store_id, campaign_id, ad_type, report_date, now):
    """Preserve the caller's evidence-approved action on the group and lead routes."""
    from sqlalchemy import select
    from jx_alert.ai.ad_review_skills import load_ad_review_skills
    from jx_alert.notifications.ad_repeat import (
        INCIDENT, campaign_identity, incident_already_recorded, incident_key, load_incident_history,
        subscriber_reservations,
    )
    from jx_alert.notifications.hourly_ads import BEIJING, delivery_window
    from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification
    from jx_alert.storage.models import SyncJob

    existing = db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.idempotency_key == key))
    if existing is not None:
        return [existing]
    evidence = dict(store_id=store_id, campaign_id=campaign_id, ad_type=ad_type, report_date=str(report_date))
    event = incident_key(evidence)
    # Legacy submitted reviews do not carry the same repeat metrics as the
    # automatic analyst. Use its conservative full-history suppression.
    if (delivery_window({'hourly_ad_monitor': True}, now)[0] != 'READY'
            or load_ad_review_skills(settings).status == 'invalid'
            or campaign_identity(evidence) in load_incident_history(db, now)
            or incident_already_recorded(db, evidence, now=now)):
        return []
    context = dict(text=text, store_id=store_id, entity_id=campaign_id, card=card,
                   fixed_route=True, material_ad_alert=True)
    group = enqueue_notification(db, idempotency_key=key, chat_id=destination['receive_id'],
                                 delivery=destination, **context)
    window = {'material_ad_delivery_window': True, 'hourly_delivery_date': now.astimezone(BEIJING).date().isoformat()}
    group.payload = {**group.payload, **window, 'ad_group_owners': True, 'ad_campaign_type': ad_type}
    ids = [group.id]
    for lead in advertising_subscriber_deliveries(settings):
        if subscriber_reservations(db, [event], lead):
            continue
        lead_key = 'app:confirmed-ad-lead:' + hashlib.sha256(key.encode()).hexdigest()
        copy = enqueue_notification(db, idempotency_key=lead_key, chat_id=lead['receive_id'],
                                    delivery=lead, **context)
        copy.payload = {**copy.payload, **window, 'ad_campaign_type': ad_type}
        ids.append(copy.id)
    db.add(SyncJob(job_type=INCIDENT, status='QUEUED', started_at=now.astimezone(timezone.utc).replace(tzinfo=None),
        metadata_json={**evidence, 'incident_key': event, 'outbox_ids': ids}))
    db.flush()
    return ids


def subscriber_entries(routing):
    """Return None for malformed subscription data; never invent recipients."""
    section = routing.get(SECTION)
    if section is None:
        return []
    if not isinstance(section, dict) or not isinstance(section.get('recipients'), list):
        return None
    entries = section['recipients']
    if len(entries) > 100:
        return None
    seen = set()
    for row in entries:
        if (not isinstance(row, dict) or not isinstance(row.get('open_id'), str)
                or not re.fullmatch(r'ou_[A-Za-z0-9_-]+', row['open_id'])
                or row['open_id'] in seen or not isinstance(row.get('generation'), str)
                or not re.fullmatch(r'[a-f0-9]{32}', row['generation'])
                or not isinstance(row.get('name'), str) or len(row['name']) > 100):
            return None
        seen.add(row['open_id'])
    return entries


def advertising_subscriber_deliveries(settings):
    try:
        routing = json.loads(Path(settings.feishu_routing_file).read_text(encoding='utf-8'))
        entries = subscriber_entries(routing) if isinstance(routing, dict) else None
    except (OSError, ValueError, TypeError):
        entries = None
    if not entries or len(entries) != 1:
        return []
    groups = routing.get('groups')
    group = groups.get(getattr(settings, 'feishu_daily_report_chat_id', '')) if isinstance(groups, dict) else None
    if not isinstance(group, dict) or not isinstance(group.get('chat_id'), str) or not group['chat_id'].startswith('oc_'):
        return []
    roster = group.get('verified_member_open_ids')
    if roster is not None and (not isinstance(roster, list) or entries[0]['open_id'] not in roster):
        return []
    return [{'role': ROLE, 'receive_id_type': 'open_id', 'receive_id': row['open_id'],
             'open_ids': [], 'subscription_generation': row['generation']}
            for row in entries]


def subscriber_delivery_blocked(settings, payload):
    """A revoked/re-added recipient cannot receive an older queued alert."""
    delivery = payload.get('delivery') or {}
    if delivery.get('role') != ROLE:
        return False
    if (payload.get('transport') != 'app' or payload.get('material_ad_alert') is not True
            or delivery.get('receive_id_type') != 'open_id'
            or payload.get('scheduled_delivery_date') or payload.get('morning_focus')
            or delivery.get('reply_to_message_id')):
        return True
    return not any(row['receive_id'] == delivery.get('receive_id')
                   and row['subscription_generation'] == delivery.get('subscription_generation')
                   for row in advertising_subscriber_deliveries(settings))
