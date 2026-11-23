"""Evidence-based repeat suppression for automatic advertising notifications."""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, tuple_

from jx_alert.analysis.hourly_ad_patterns import HIGH_LOW, LOW_ZERO, OPPORTUNITY
from jx_alert.notifications.outbox import NotificationOutbox, RESERVING
from jx_alert.storage.models import Campaign, Store, SyncJob


INCIDENT = 'hourly_ad_incident'
COOLDOWN = timedelta(hours=72)
REVIEW_INTERVAL = timedelta(days=7)
_ABSOLUTE_INTRADAY_SIGNALS = frozenset({'intraday_budget', 'intraday_no_clicks'})

_FAMILIES = {
    'CTR': 'traffic', 'CPC': 'traffic',
    'CDR': 'conversion', 'CPS': 'conversion', 'CPA': 'conversion', 'ROAS': 'conversion',
    'orders': 'conversion', 'zero_conversion': 'conversion',
    'intraday_budget': 'budget',
    'intraday_no_clicks': 'intraday_traffic',
    HIGH_LOW: 'conversion', LOW_ZERO: 'conversion', OPPORTUNITY: 'opportunity',
}
# Increase in adverse difference from the comparison, in each metric's units.
_ADDITIONAL_IMPACT = {
    'CTR': .002, 'CDR': .02,  # 0.2 and 2 percentage points
    'CPC': .25, 'CPA': 5, 'CPS': 5, 'ROAS': .5, 'orders': 3,
    'zero_conversion': 30, 'intraday_budget': 30, 'intraday_no_clicks': 10000,
    HIGH_LOW: 30, LOW_ZERO: 30, OPPORTUNITY: 1,
}
_NEGATIVE_DIRECTION = {'CTR', 'CDR', 'ROAS', 'orders'}


def campaign_identity(row):
    return row['store_id'], row['ad_type'], row['campaign_id']


def incident_key(row):
    identity = [row['store_id'], row['campaign_id'], row['ad_type'], row['report_date'],
                'intraday' if row.get('intraday') else
                'daily_opportunity' if row.get('category') == 'opportunity' else 'daily_review']
    return 'hourly-ad:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _outbox_ids(item):
    raw = item.get('outbox_ids') if isinstance(item, dict) else None
    return [oid for oid in raw if type(oid) is int and oid > 0] if isinstance(raw, list) else []


def _group_outboxes(db, incident_items):
    ids = {oid for item in incident_items for oid in _outbox_ids(item)}
    return {oid: (status, payload) for oid, status, payload in db.execute(
        select(NotificationOutbox.id, NotificationOutbox.status, NotificationOutbox.payload).where(
            NotificationOutbox.id.in_(ids),
            NotificationOutbox.payload['delivery']['receive_id_type'].as_string() == 'chat_id',
            NotificationOutbox.status.in_((*RESERVING, 'DELIVERED')),
    ))} if ids else {}


def _stored_identity(item):
    return item.get('store_id'), item.get('ad_type', 'sp'), item.get('campaign_id')


def _outbox_reserves(state, payload, item):
    planned = payload.get('hourly_items')
    if isinstance(planned, list) and not any(
            isinstance(row, dict) and _stored_identity(row) == _stored_identity(item)
            and row.get('category') == item.get('category') for row in planned):
        return False
    if state in RESERVING:
        return True
    if state != 'DELIVERED':
        return False
    # Older deliveries had no per-activity receipt. Preserve their reservation.
    if 'hourly_delivered_items' not in payload:
        return True
    sent = payload['hourly_delivered_items']
    return isinstance(sent, list) and any(isinstance(row, dict)
        and _stored_identity(row) == _stored_identity(item)
        and row.get('category') == item.get('category') for row in sent)


def intraday_observation_is_recent(evidence, now):
    if not isinstance(evidence, dict):
        return False
    try:
        captured = datetime.fromisoformat(evidence["captured_at"])
        return captured.tzinfo is not None and timedelta(0) <= now - captured <= timedelta(minutes=75)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def live_observation_current(item, campaign, store, now):
    """Shared current-day/age/budget policy for held or outgoing observations."""
    evidence = item.get("live_observation")
    if evidence is None:
        return True  # Older drafts have no new observation metadata.
    if not intraday_observation_is_recent(evidence, now):
        return False
    try:
        report_day = now.astimezone(ZoneInfo(store.timezone)).date().isoformat()
    except (TypeError, ValueError, ZoneInfoNotFoundError, OverflowError):
        return False
    budget = evidence.get("daily_budget")
    return evidence.get("day") == report_day and (budget is None or budget == campaign.budget)


def _held_campaigns(db, candidates, now):
    ids = {
        _stored_identity(item)
        for status, item in candidates
        if status == "HELD" and isinstance(item, dict)
        and isinstance(item.get("hourly_item"), dict)
        and intraday_observation_is_recent(item["hourly_item"].get("live_observation"), now)
    }
    return {
        (campaign.store_id, campaign.campaign_type, campaign.campaign_id): (campaign, store)
        for campaign, store in db.execute(select(Campaign, Store).join(Store, Store.sid == Campaign.store_id).where(
            Store.status == 1, Campaign.state == "enabled",
            tuple_(Campaign.store_id, Campaign.campaign_type, Campaign.campaign_id).in_(ids),
        ))
    } if ids else {}


def _reserved_incident(status, item, outboxes, today, now, held_campaigns):
    if not isinstance(item, dict) or not item.get('store_id') or not item.get('campaign_id'):
        return False
    if status == 'HELD':
        release = _day(item.get('release_day'))
        if release is None or release < today:
            return False
        hourly_item = item.get("hourly_item")
        if isinstance(hourly_item, dict) and hourly_item.get("live_observation") is not None:
            # Undeliverable intraday holds are not deliveries; letting them
            # reserve the campaign suppresses the fresh evidence at opening.
            current = held_campaigns.get(_stored_identity(item))
            return bool(current and live_observation_current(hourly_item, *current, now))
        return True
    return any(_outbox_reserves(*outboxes.get(oid, (None, {})), item) for oid in _outbox_ids(item))


def subscriber_reservations(db, incident_keys, destination):
    """Avoid a second private copy when a new group attempt is needed."""
    keys = {key for key in incident_keys if key}
    if not keys:
        return set()
    incidents = list(db.scalars(select(SyncJob.metadata_json).where(
        SyncJob.job_type == INCIDENT, SyncJob.status == 'QUEUED',
        SyncJob.metadata_json['incident_key'].as_string().in_(keys))))
    ids = {oid for item in incidents for oid in _outbox_ids(item)}
    if not ids:
        return set()
    outboxes = {oid: (status, payload) for oid, status, payload in db.execute(
        select(NotificationOutbox.id, NotificationOutbox.status, NotificationOutbox.payload).where(
            NotificationOutbox.id.in_(ids), NotificationOutbox.chat_id == destination['receive_id'],
            NotificationOutbox.status.in_((*RESERVING, 'DELIVERED'))))
        if payload.get('delivery') == destination}
    return {item['incident_key'] for item in incidents
            if any(_outbox_reserves(*outboxes.get(oid, (None, {})), item) for oid in _outbox_ids(item))}


def incident_already_recorded(db, row, *, now=None):
    """Check whether the exact source day is reserved or reached the group."""
    clock = now or datetime.now(timezone.utc)
    today = clock.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    candidates = db.execute(select(SyncJob.status, SyncJob.metadata_json).where(
        SyncJob.job_type == INCIDENT, SyncJob.status.in_(('HELD', 'QUEUED')),
        SyncJob.metadata_json['incident_key'].as_string() == incident_key(row),
    )).all()
    outboxes = _group_outboxes(db, (item for _, item in candidates))
    held = _held_campaigns(db, candidates, clock)
    return any(_reserved_incident(status, item, outboxes, today, clock, held)
               for status, item in candidates)


def load_incident_history(db, now):
    """Return group reservations and receipts from the past seven days."""
    end = now.astimezone(timezone.utc).replace(tzinfo=None)
    today = now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    rows = db.execute(select(SyncJob.started_at, SyncJob.status, SyncJob.metadata_json).where(
        SyncJob.job_type == INCIDENT, SyncJob.status.in_(('HELD', 'QUEUED')),
        SyncJob.started_at > end - REVIEW_INTERVAL, SyncJob.started_at <= end,
    ).order_by(SyncJob.started_at.desc(), SyncJob.id.desc())).all()
    outboxes = _group_outboxes(db, (item for _, _, item in rows))
    history = defaultdict(list)
    held = _held_campaigns(db, ((status, item) for _, status, item in rows), now)
    for started_at, status, item in rows:
        if _reserved_incident(status, item, outboxes, today, now, held):
            history[_stored_identity(item)].append((started_at.replace(tzinfo=timezone.utc), item))
    return history


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _day(value):
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def _measure(row, code):
    """Compare only comparable, explicitly documented impacts from evidence."""
    mature = row.get('mature') or {}
    baseline = row.get('baseline') or {}
    if code in {HIGH_LOW, LOW_ZERO}:
        pattern = row.get('hourly_pattern') or {}
        amount = pattern.get('estimated_excess_spend' if code == HIGH_LOW else 'spend')
        if pattern.get('code') == code and _number(amount):
            return code, _day(row.get('mature_date')), amount, None
        return None
    if code == 'zero_conversion':
        amount = mature.get('spend')
        orders = mature.get('orders')
        if _number(orders) and orders == 0 and _number(amount):
            return 'mature_spend', _day(row.get('mature_date')), amount, None
        return None
    if code == 'intraday_no_clicks':
        current = row.get('current') or {}
        amount, clicks, impressions = (current.get(key) for key in ('spend', 'clicks', 'impressions'))
        if (_number(amount) and amount == 0 and _number(clicks) and clicks == 0
                and _number(impressions) and impressions > 0):
            return 'intraday_zero_click_impressions', _day(row.get('report_date')), impressions, None
        return None
    if code == 'intraday_budget':
        value = (row.get('current') or {}).get('spend')
        reference = row.get('verified_daily_budget')
        basis, day = 'source_budget', row.get('report_date')
    elif code == 'orders':
        value, reference = mature.get('orders'), baseline.get('orders')
        basis, day = baseline.get('basis'), row.get('mature_date')
    else:
        metric = (row.get('kpis') or {}).get(code) or {}
        value, reference = metric.get('current'), metric.get('baseline')
        basis, day = metric.get('comparison_basis'), metric.get('current_date')
    if not basis or not _number(value) or not _number(reference) or reference <= 0:
        return None
    impact = reference - value if code in _NEGATIVE_DIRECTION else value - reference
    if impact <= 0 or not math.isfinite(impact / reference):
        return None
    return basis, _day(day), impact, impact / reference


def incident_metadata(row, signals):
    """Keep just the numeric comparator, not the source response or model text."""
    return {
        'incident_key': incident_key(row), 'store_id': row['store_id'], 'ad_type': row['ad_type'],
        'campaign_id': row['campaign_id'], 'report_date': row['report_date'], 'currency': row.get('currency'),
        'intraday': row.get('intraday') is True,
        **({'category': 'opportunity'} if row.get('category') == 'opportunity' else {}),
        'risk_metrics': {code: _measure(row, code) for code in signals},
    }


def _worsened(current, previous, code):
    if not current or not isinstance(previous, (tuple, list)) or len(previous) != 4:
        return False
    basis, day, impact, ratio = current
    prior_basis, prior_day, prior_impact, prior_ratio = previous
    if not _number(prior_impact) or basis != prior_basis or not day or not _day(prior_day) or day <= prior_day:
        return False
    # Quantitative deterioration needs both additional impact and ≥25% worse
    # impact. When a denominator exists, also require ≥10 percentage points.
    if impact - prior_impact + 1e-9 < _ADDITIONAL_IMPACT[code] or impact < prior_impact * 1.25:
        return False
    return ratio is None or (_number(prior_ratio) and ratio - prior_ratio + 1e-9 >= .10)


def _earlier_daily_incident(item, report_day):
    """Classify only proven completed-day history, including known older receipts."""
    prior_day = _day(item.get('report_date'))
    intraday = item.get('intraday')
    if (prior_day is None or prior_day >= report_day
            or intraday is not None and intraday is not False):
        return False
    metrics = item.get('risk_metrics')
    if isinstance(metrics, dict) and metrics:
        return all(code in _FAMILIES and code not in _ABSOLUTE_INTRADAY_SIGNALS for code in metrics)
    return item.get('origin') == 'daily_report'


def repeat_eligible_signals(row, signals, history, now):
    """Keep ordinary cooldowns and allow one independently verified intraday risk."""
    opportunity = list(signals) == [OPPORTUNITY]
    previous = [(stamp, item) for stamp, item in history.get(campaign_identity(row), ())
                if (item.get('category') == 'opportunity') == opportunity]
    if not previous:
        return list(signals)
    report_day = _day(row.get('report_date'))
    if report_day is None:
        return []
    if now - previous[0][0] < COOLDOWN:
        if row.get('intraday') is not True or not all(_earlier_daily_incident(prior, report_day)
                for stamped, prior in previous if now - stamped < COOLDOWN):
            return []
        return [code for code in signals if code in _ABSOLUTE_INTRADAY_SIGNALS and _measure(row, code)]
    accepted = []
    for code in signals:
        family = _FAMILIES.get(code)
        if family is None:
            continue
        # Old incidents cannot prove an earlier metric or family. Keep them
        # quiet until they age out rather than inferring that the signal changed.
        for _, prior in previous:
            metrics = prior.get('risk_metrics')
            if not isinstance(metrics, dict):
                break
            prior_date = _day(prior.get('report_date'))
            if not prior_date or report_day <= prior_date:
                break
            if any(_FAMILIES.get(seen) == family for seen in metrics):
                if row.get('currency') == prior.get('currency') and _worsened(_measure(row, code), metrics.get(code), code):
                    accepted.append(code)
                break
        else:
            accepted.append(code)
    return accepted
