"""Read-only, source-matched campaign evidence for a question-led analyst.

Only the service decides whether facts are complete and mature. The analyst can
select and explain material adverse signals; it cannot fill gaps or write ads.
MCP UI campaign observations currently lack attributed orders/sales and must not
be combined with the API reporting grain to manufacture conversion evidence.
"""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.ai.ad_review_skills import DEFAULT_SKILL, load_ad_review_skills
from jx_alert.analysis.ad_kpis import compare_kpis
from jx_alert.analysis.materiality import POLICY, material_change
from jx_alert.lingxing.attribution import AD_ATTRIBUTION_DAYS
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, RawSnapshot


_CORE = ("spend", "orders", "sales", "clicks", "impressions")
_CURRENT = ("spend", "clicks", "impressions")
_FIELDS = (*_CORE, "units")
_KPI_FIELDS = ("label", "current", "baseline", "current_date", "currency", "format",
               "change_ratio", "comparison_basis", "comparison_dates", "formula", "status")
_STATES = {"enabled": "投放中", "paused": "已暂停", "archived": "已归档"}
_RAW_FIELDS = {"spend": "cost", **{key: key for key in _FIELDS if key != "spend"}}
_TRAFFIC_SIGNALS = {"CTR", "CPC", "spend"}
MIN_CTR_EXPECTED_CLICK_GAP = 10


def campaign_start_day(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return None


def ctr_expected_click_gap(row):
    """Estimate the gap at this day's exposure using the verified reference CTR."""
    traffic = row.get('current', {})
    reference = row.get('kpis', {}).get('CTR', {}).get('baseline')
    impressions, clicks = traffic.get('impressions'), traffic.get('clicks')
    if not all(_valid(value) for value in (reference, impressions, clicks)) or reference > 1 or clicks > impressions:
        return None
    return max(0.0, reference * impressions - clicks)


def automatic_sp_signals(row, *, has_mature_history=False):
    """Use established campaign evidence for proactive ads, including intraday."""
    skill = row.get('ad_skill') or DEFAULT_SKILL
    if skill.get('status') == 'invalid':
        return []
    start = campaign_start_day(row.get('campaign_start_date'))
    report = date.fromisoformat(row['report_date'])
    lag = AD_ATTRIBUTION_DAYS.get(row.get('ad_type'), AD_ATTRIBUTION_DAYS['sp'])
    learning = max(lag, skill['learning_days'])
    if start is not None and (report - start).days < learning:
        return []
    if start is None and learning > lag:
        established = [date.fromisoformat(x) for x in row.get('evidence_dates', {})]
        if not established or (report - min(established)).days < learning:
            return []
    if row.get('intraday'):
        spend = row.get('current', {}).get('spend')
        budget = row.get('verified_daily_budget')
        # Amazon's average daily budget may use up to 100% more from other
        # days. A source event alone does not establish a material overrun.
        if (not (start is not None or has_mature_history) or not _valid(spend) or not _valid(budget)
                or budget == 0 or spend < budget * skill['intraday_budget_multiple']
                or spend - 2 * budget < skill['intraday_overage_usd']):
            return []
        return row['material_signals']
    # Raw spend growth alone can be planned scaling. Report the evidence-backed
    # efficiency or conversion problem that warrants an operator's attention.
    gap = ctr_expected_click_gap(row) if 'CTR' in row['material_signals'] else None
    return [code for code in row['material_signals'] if code != 'spend'
            and (code != 'CTR' or gap is not None and gap >= MIN_CTR_EXPECTED_CLICK_GAP)]


def _utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _end(day, zone, delay=0):
    return datetime.combine(day + timedelta(days=delay + 1), time.min, zone).astimezone(timezone.utc)


def _period(observations, days):
    keys = list(_CORE)
    if all("units" in observations[day] for day in days):
        keys.append("units")
    return {metric: math.fsum(observations[day][metric] for day in days) / len(days) for metric in keys}


def _sample_passes(observed, on, prior, signal, skill):
    fields = {"impressions": skill["min_impressions"]} if signal == "CTR" else {
        "clicks": skill["min_clicks"], "spend": skill["min_spend_usd"]}
    current = observed[on]
    reference = [observed.get(day, {}) for day in prior]
    return bool(reference) and all(current.get(key, 0) >= floor
        and all(_valid(r.get(key)) for r in reference)
        and math.fsum(r[key] for r in reference) / len(reference) >= floor for key, floor in fields.items())


def _zero_outcome(row):
    return row["orders"] == 0 and row["sales"] == 0 and row.get("units", 0) == 0


def _mature_signals(observed, day, baseline, prior, skill):
    current = observed[day]
    # Disagreement between orders, units and sales is not verified zero.
    if (not _sample_passes(observed, day, prior, "orders", skill)
            or any(not observed[ref]["orders"] and not _zero_outcome(observed[ref]) for ref in prior)):
        return {}, []
    rate = baseline["orders"] / baseline["clicks"]
    expected = current["clicks"] * rate
    minimum = skill["min_expected_orders"]
    if len(prior) == 1 or skill["objective"] in {"reach", "discovery"}:
        minimum *= 1.5
    proof = {"reference_click_conversion_rate": rate, "expected_orders_at_reference_rate": expected,
             "min_expected_orders": minimum, "basis": "SEVEN_DAY" if len(prior) == 7 else "PREVIOUS_DAY"}
    if not math.isfinite(expected) or (current["orders"] == 0 and not _zero_outcome(current)):
        return {}, []
    signals = []
    if _zero_outcome(current) and expected >= minimum and material_change(observed, day, "zero_conversion"):
        signals.append("zero_conversion")
    # Lower order volume explained by fewer clicks is not evidence of an
    # efficiency issue. ROAS and CPA are tested separately against their data.
    if (current["orders"] > 0 and rate > 0 and material_change(observed, day, "orders")
            and current["orders"] / current["clicks"] <= rate * (1 - skill["min_relative_change"])):
        signals.append("orders")
    return proof, signals


def _store_campaigns(db, settings, store, target, kinds, clock, skills):
    try:
        zone = ZoneInfo(store.timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return []
    if store.status != 1 or target >= clock.astimezone(zone).date():
        return []
    first = target - timedelta(days=max(AD_ATTRIBUTION_DAYS[k] for k in kinds) + 8)
    dimensions = ["campaign_" + kind for kind in kinds]
    facts = list(db.scalars(select(DailyMetric).where(DailyMetric.store_id == str(store.sid),
        DailyMetric.dimension.in_(dimensions), DailyMetric.metric_date.between(first, target),
        DailyMetric.metric_name.in_(_FIELDS)).order_by(DailyMetric.id)))
    coverage = {(row.dimension, row.metric_date): row for row in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == str(store.sid), DataCoverage.dimension.in_(dimensions),
        DataCoverage.metric_date.between(first, target)))}
    snapshots = {}
    for snapshot in db.scalars(select(RawSnapshot).where(RawSnapshot.store_id == str(store.sid),
            RawSnapshot.source.in_(["lingxing_ads_" + kind + "_campaign_report" for kind in kinds]),
            RawSnapshot.payload["report_date"].as_string().between(first.isoformat(), target.isoformat()))
            .order_by(RawSnapshot.id.desc())):
        payload = snapshot.payload or {}
        if isinstance(payload, dict):
            key = snapshot.source, payload.get("report_date")
            if key not in snapshots:
                snapshots[key] = snapshot
    partitions, entities = {}, {}
    for row in facts:
        key = row.dimension, row.metric_date
        partitions.setdefault((*key, row.entity_id), []).append(row)
        entities.setdefault(key, set()).add(row.entity_id)

    def evidence(campaign, on, *, mature):
        dimension = "campaign_" + campaign.campaign_type
        cov = coverage.get((dimension, on))
        count = len(entities.get((dimension, on), set()))
        captured = _utc(cov.completed_at) if cov else None
        delay = AD_ATTRIBUTION_DAYS[campaign.campaign_type] if mature else 0
        if (cov is None or cov.status != "COMPLETE" or cov.row_count <= 0 or cov.row_count != count
                or captured is None or captured < _end(on, zone, delay) or captured > clock):
            return None
        rows = partitions.get((dimension, on, campaign.campaign_id), [])
        required = _CORE if mature else _CURRENT
        by_metric = {row.metric_name: row for row in rows}
        if (not all(metric in by_metric and _valid(by_metric[metric].value) for metric in required)
                or any(row.source != "lingxing_ads_" + campaign.campaign_type for row in rows)):
            return None
        snapshot = snapshots.get(("lingxing_ads_" + campaign.campaign_type + "_campaign_report", on.isoformat()))
        observed = _utc(snapshot.captured_at) if snapshot else None
        if observed is None or observed < _end(on, zone, delay) or observed > clock:
            return None
        payload = snapshot.payload
        raw_rows = payload.get("rows")
        if (payload.get("status") not in (None, "COMPLETE", "SUCCEEDED")
                or payload.get("report_type") != campaign.campaign_type or not isinstance(raw_rows, list)
                or len(raw_rows) != cov.row_count):
            return None
        matches = [row for row in raw_rows if isinstance(row, dict)
                   and str(row.get("campaign_id")) == campaign.campaign_id]
        if len(matches) != 1:
            return None
        raw = matches[0]
        try:
            if any(isinstance(raw.get(_RAW_FIELDS[key]), bool)
                    or float(raw[_RAW_FIELDS[key]]) != by_metric[key].value for key in required):
                return None
        except (KeyError, ValueError, TypeError, OverflowError):
            return None
        money = [by_metric[key].currency for key in ("spend", "sales") if key in required]
        if len(set(money)) != 1 or money[0] != "USD":
            return None
        values = {key: by_metric[key].value for key in required}
        if "units" in by_metric and _valid(by_metric["units"].value) and mature:
            try:
                if not isinstance(raw.get("units"), bool) and float(raw["units"]) == by_metric["units"].value:
                    values["units"] = by_metric["units"].value
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
        values.update(currency=money[0], sales_currency=money[0])
        return values, observed.isoformat(), {"snapshot": [snapshot.id, observed.isoformat()],
            "coverage": [cov.id, captured.isoformat()],
            "facts": [(row.id, row.metric_name, row.value, row.currency) for row in rows]}

    result = []
    campaigns = db.scalars(select(Campaign).where(Campaign.store_id == str(store.sid),
        Campaign.campaign_type.in_(kinds)).order_by(Campaign.campaign_type, Campaign.campaign_id))
    for campaign in campaigns:
        skill = skills.for_campaign(store.sid, campaign.campaign_type, campaign.campaign_id)
        lag = AD_ATTRIBUTION_DAYS[campaign.campaign_type]
        mature_day = target - timedelta(days=lag)
        current, mature = evidence(campaign, target, mature=False), evidence(campaign, mature_day, mature=True)
        if current is None or mature is None:
            continue
        observed, proof, capture = {}, {}, {}
        for offset in range((target - first).days + 1):
            day = target - timedelta(days=offset)
            # Recent periods supply only non-conversion facts. Conversion dates
            # must have been re-observed after their own attribution deadline.
            value = evidence(campaign, day, mature=day <= mature_day)
            if value:
                observed[day], capture[day.isoformat()], proof[day.isoformat()] = value
        seven = [mature_day - timedelta(days=i) for i in range(1, 8)]
        baseline_days = seven if all(day in observed for day in seven) else [seven[0]]
        if not all(day in observed for day in baseline_days):
            continue
        # Current cost/click quality needs its own adjacent full-day evidence.
        if target - timedelta(days=1) not in observed:
            continue
        kpis = compare_kpis(observed, target, maturity_days=lag, store_scope=False,
            min_clicks=getattr(settings, "alert_min_clicks", 10),
            min_impressions=getattr(settings, "alert_kpi_min_impressions", 1000),
            min_spend=getattr(settings, "alert_min_spend", 20),
            ratio=getattr(settings, "alert_kpi_change_ratio", .15))
        baseline = _period(observed, baseline_days)
        mature_comparison, signals = _mature_signals(observed, mature_day, baseline, baseline_days, skill)
        # A sparse all-zero outcome must not sneak back in as ROAS or CDR.
        # A verified zero outcome has its own specific signal.
        mature_kpi_ready = mature[0]["orders"] > 0 and bool(mature_comparison)
        for code, kpi in kpis.items():
            if kpi["status"] == "ADVERSE":
                on = date.fromisoformat(kpi["current_date"])
                references = [date.fromisoformat(value) for value in kpi["comparison_dates"][1:]]
                if ((code in _TRAFFIC_SIGNALS or mature_kpi_ready)
                        and abs(kpi["change_ratio"]) + 1e-10 >= skill["min_relative_change"]
                        and _sample_passes(observed, on, references, code, skill)
                        and material_change(observed, on, code)):
                    signals.append(code)
                else:
                    kpi["status"] = "WATCH"
        selected_kpis = {code: {key: item[key] for key in _KPI_FIELDS if key in item}
            for code, item in kpis.items() if item["current"] is not None and item["baseline"] is not None
            and item["status"] not in {"SCOPE_UNSUPPORTED", "CURRENCY_UNCONFIRMED"}}
        state_at = _utc(campaign.updated_at)
        state = _STATES.get((campaign.state or "").lower(), "状态未确认") if state_at and state_at <= clock else "状态未确认"
        row = {"store_id": str(store.sid), "store_name": store.name,
            "campaign_id": campaign.campaign_id, "campaign_name": campaign.name or campaign.campaign_id,
            "ad_type": campaign.campaign_type, "current_state": state,
            "campaign_start_date": campaign.start_date,
            "state_observed_at": state_at.isoformat() if state_at and state_at <= clock else None,
            "report_date": target.isoformat(), "mature_date": mature_day.isoformat(),
            "attribution_days": lag, "currency": "USD", "source": "lingxing_ads_" + campaign.campaign_type,
            "current": {key: current[0][key] for key in _CURRENT},
            "mature": {key: mature[0][key] for key in _FIELDS if key in mature[0]},
            "baseline": {"basis": "SEVEN_DAY" if len(baseline_days) == 7 else "PREVIOUS_DAY",
                "dates": [day.isoformat() for day in baseline_days], **baseline},
            "ad_skill": skill, "mature_comparison": mature_comparison,
            "kpis": selected_kpis, "material_signals": signals,
            "evidence_dates": capture}
        row["evidence_id"] = hashlib.sha256(json.dumps({"row": row, "facts": proof},
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        result.append(row)
    return result


def build_ad_review_context(db, settings, stores, report_dates, *, ad_types=("sp",), now=None, skills=None):
    """Freeze every eligible campaign in an already-authorized scope, with no writes.

    Dates remain marketplace-local. Callers batch all returned candidates instead
    of silently truncating to a few high-spend activities. Excluded datasets never
    enter the analyst context as gaps or fabricated normal/zero observations.
    """
    kinds = tuple(dict.fromkeys(ad_types))
    if not kinds or any(kind not in AD_ATTRIBUTION_DAYS for kind in kinds):
        raise ValueError("Unsupported advertising type")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Advertising reviews require an aware clock")
    clock = clock.astimezone(timezone.utc)
    skills = skills or load_ad_review_skills(settings)
    campaigns = []
    for store in stores:
        raw = report_dates.get(str(store.sid))
        if raw is None:
            continue
        target = date.fromisoformat(raw) if isinstance(raw, str) else raw
        if not isinstance(target, date) or isinstance(target, datetime):
            raise ValueError("Advertising report dates must be dates")
        campaigns.extend(_store_campaigns(db, settings, store, target, kinds, clock, skills))
    return {"ad_review": True, "reply_mode": "answer", "campaigns": campaigns,
        "policy": {**POLICY, "scope": "仅当前可见、完整且归因成熟的同源广告活动",
            "ad_types": list(kinds), "advertising_changes_allowed": False,
            "skills_status": skills.status,
            "analysis": "结合原始问题选取有实质影响的异常；正常、缺失和未成熟数据不出现在回复中。"
                        "不同统计日期、广告粒度、来源不得拼接；花费增加本身不等于浪费。"}}
