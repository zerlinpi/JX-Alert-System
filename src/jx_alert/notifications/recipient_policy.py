"""Explicit extra private recipients for material advertising alerts only."""
import json
from pathlib import Path
import re


SECTION = 'ad_alert_subscribers'
ROLE = 'ad_alert_subscriber'
AD_SCOPES = frozenset({'campaign_sp', 'campaign_sb', 'campaign_sd', 'product_ad_sp', 'product_ad_sd'})


def advertising_scope(scope, store_id):
    return scope in AD_SCOPES or scope in {
        f'{store_id}:{family}:{kind}' for family in ('campaign', 'product_ad') for kind in ('sp', 'sb', 'sd')}


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
    return [{'role': ROLE, 'receive_id_type': 'open_id', 'receive_id': row['open_id'],
             'open_ids': [], 'subscription_generation': row['generation']}
            for row in entries or []]


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
