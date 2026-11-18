"""Evidence-based repeat suppression for automatic advertising notifications."""

from collections import defaultdict
from datetime import date, timedelta, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from sqlalchemy import select

from jx_alert.storage.models import SyncJob


INCIDENT = 'hourly_ad_incident'
COOLDOWN = timedelta(hours=72)
REVIEW_INTERVAL = timedelta(days=7)

_FAMILIES = {
    'CTR': 'traffic', 'CPC': 'traffic',
    'CDR': 'conversion', 'CPS': 'conversion', 'CPA': 'conversion', 'ROAS': 'conversion',
    'orders': 'conversion', 'zero_conversion': 'conversion',
    'intraday_budget': 'budget',
}
# Increase in adverse difference from the comparison, in each metric's units.
_ADDITIONAL_IMPACT = {
    'CTR': .002, 'CDR': .02,  # 0.2 and 2 percentage points
    'CPC': .25, 'CPA': 5, 'CPS': 5, 'ROAS': .5, 'orders': 3,
    'zero_conversion': 30, 'intraday_budget': 30,
}
_NEGATIVE_DIRECTION = {'CTR', 'CDR', 'ROAS', 'orders'}


def campaign_identity(row):
    return row['store_id'], row['ad_type'], row['campaign_id']


def incident_key(row):
    identity = [row['store_id'], row['campaign_id'], row['ad_type'], row['report_date'],
                'intraday' if row.get('intraday') else 'daily_review']
    return 'hourly-ad:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def incident_already_recorded(db, row):
    """The exact source day/window is not replayed after the cooldown."""
    return db.scalar(select(SyncJob.id).where(SyncJob.job_type == INCIDENT,
        SyncJob.metadata_json['incident_key'].as_string() == incident_key(row)).limit(1)) is not None


def load_incident_history(db, now):
    """Return one bounded cross-channel history for this analysis batch."""
    end = now.astimezone(timezone.utc).replace(tzinfo=None)
    today = now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    rows = db.execute(select(SyncJob.started_at, SyncJob.status, SyncJob.metadata_json).where(
        SyncJob.job_type == INCIDENT, SyncJob.status.in_(('HELD', 'QUEUED')),
        SyncJob.started_at > end - REVIEW_INTERVAL, SyncJob.started_at <= end,
    ).order_by(SyncJob.started_at.desc(), SyncJob.id.desc()))
    history = defaultdict(list)
    for started_at, status, item in rows:
        if isinstance(item, dict) and item.get('store_id') and item.get('campaign_id'):
            release = _day(item.get('release_day'))
            if status == 'HELD' and release and release < today:
                continue
            identity = item['store_id'], item.get('ad_type', 'sp'), item['campaign_id']
            history[identity].append((started_at.replace(tzinfo=timezone.utc), item))
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
    if code == 'zero_conversion':
        amount = mature.get('spend')
        orders = mature.get('orders')
        if _number(orders) and orders == 0 and _number(amount):
            return 'mature_spend', _day(row.get('mature_date')), amount, None
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


def repeat_eligible_signals(row, signals, history, now):
    """Enforce per-activity cooldown, then verify changes within each family."""
    previous = history.get(campaign_identity(row), ())
    if not previous:
        return list(signals)
    if now - previous[0][0] < COOLDOWN:
        return []
    report_day = _day(row.get('report_date'))
    if report_day is None:
        return []
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
