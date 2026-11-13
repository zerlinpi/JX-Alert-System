"""Coverage-aware deterministic evaluation of daily sales and advertising metrics."""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from jx_alert.analysis.rules import (
    RuleConfig,
    _event,
    detect_campaign_anomalies,
    detect_order_anomaly,
)
from jx_alert.lingxing.attribution import AD_ATTRIBUTION_DAYS
from jx_alert.analysis.service import enqueue_anomaly_notifications, upsert_anomalies
from jx_alert.storage.models import (
    AdProduct,
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    Product,
    Store,
)


def _parse_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _comparison_evidence(db, store_id, dimension, dates):
    rows = db.scalars(select(DailyMetric).where(
        DailyMetric.store_id == store_id, DailyMetric.dimension == dimension,
        DailyMetric.metric_date.in_(dates),
    )).all()
    covered = {r.metric_date for r in db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store_id, DataCoverage.dimension == dimension,
        DataCoverage.metric_date.in_(dates), DataCoverage.status == "COMPLETE"))}
    values, currencies = {}, {}
    for row in rows:
        values.setdefault(row.metric_name, {})[(row.entity_id, row.metric_date)] = row.value
        currencies.setdefault(row.metric_name, {})[(row.entity_id, row.metric_date)] = row.currency
    return values, currencies, covered


def _comparison(values, currencies, covered, entity_id, metric, target):
    from jx_alert.analysis.comparison import metric_comparison
    observations = {day: value for (entity, day), value in values.get(metric, {}).items()
                    if entity == entity_id}
    money = metric in {"sales", "spend"}
    units = ({day: code for (entity, day), code in currencies.get(metric, {}).items()
              if entity == entity_id} if money else None)
    return metric_comparison(observations, target, covered, currencies=units)


def _ready(plan):
    return plan["comparison_basis"] in {"SEVEN_DAY", "PREVIOUS_DAY"}


def _prior(values, entity, metric, target, plan):
    return ([values[metric][(entity, target - timedelta(days=i))] for i in range(1, 8)]
            if plan["comparison_basis"] == "SEVEN_DAY" else [])


def _observed(values, covered, entity, metric, day):
    import math
    value = values.get(metric, {}).get((entity, day))
    if day not in covered or value is None or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(float(value)) and value >= 0 else None


def _evaluate_sales_dimension(db, store_id: str, dimension: str,
                              entities: list[str], report_date: date,
                              config: RuleConfig, *, skip_details=None) -> tuple[list[AnomalyEvent], bool]:
    dates = [report_date - timedelta(days=i) for i in range(8)]
    values, currencies, covered = _comparison_evidence(db, store_id, dimension, dates)
    events, recovery_entities = [], set()
    complete = bool(entities)
    for entity in entities:
        entity_complete = True
        for metric in ("orders", "units", "sales"):
            plan = _comparison(values, currencies, covered, entity, metric, report_date)
            if not _ready(plan):
                entity_complete = complete = False
                if skip_details is not None:
                    skip_details.append({"store_id": store_id, "dimension": dimension,
                        "entity_id": entity, "metric_name": metric,
                        "reason": plan["comparison_basis"], "metric_date": report_date.isoformat()})
                continue
            detected = detect_order_anomaly(entity_key=entity, current_orders=plan["current"],
                previous_orders=plan["previous_day"], prior_7_days=_prior(values, entity, metric, report_date, plan),
                config=config, metric_name=metric, comparison_basis=plan["comparison_basis"])
            for event in detected:
                event["metrics"].update(comparison_dates=plan["comparison_dates"],
                                         report_date=report_date.isoformat())
                if metric == "sales":
                    event["metrics"]["currency_code"] = currencies[metric][(entity, report_date)]
            events.extend(detected)
        if entity_complete:
            recovery_entities.add(entity)
    saved = upsert_anomalies(db, store_id, events,
        evaluation_scope=f"{store_id}:sales:{dimension}", scope_complete=complete,
        recovery_entity_ids=recovery_entities, escalation_after_days=config.escalate_after_days)
    return saved, complete


def _ad_entity_events(*, values, currencies, covered, entity_id, report_date, attribution_days,
                      campaign_enabled, daily_budget, config):
    mature_day = report_date - timedelta(days=attribution_days)
    spend = _comparison(values, currencies, covered, entity_id, "spend", report_date)
    mature_spend = _comparison(values, currencies, covered, entity_id, "spend", mature_day)
    mature_orders = _comparison(values, currencies, covered, entity_id, "orders", mature_day)
    clicks = _observed(values, covered, entity_id, "clicks", report_date)
    mature_clicks = _observed(values, covered, entity_id, "clicks", mature_day)
    events = []
    if campaign_enabled and _ready(mature_orders):
        detected = detect_order_anomaly(entity_key=entity_id,
            current_orders=mature_orders["current"], previous_orders=mature_orders["previous_day"],
            prior_7_days=_prior(values, entity_id, "orders", mature_day, mature_orders),
            config=config, comparison_basis=mature_orders["comparison_basis"])
        for event in detected:
            event["metrics"].update(attribution_window_days=attribution_days,
                attribution_complete_report_date=mature_day.isoformat(),
                comparison_dates=mature_orders["comparison_dates"])
        events.extend(detected)
    if _ready(spend):
        detected = detect_campaign_anomalies(entity_key=entity_id, spend=spend["current"], orders=None,
            clicks=clicks, prior_7_spend=_prior(values, entity_id, "spend", report_date, spend),
            prior_7_orders=[], daily_budget=daily_budget, config=config,
            campaign_enabled=campaign_enabled, conversion_enabled=False,
            previous_spend=spend["previous_day"], comparison_basis=spend["comparison_basis"])
        for event in detected:
            event["metrics"].update(report_date=report_date.isoformat(),
                comparison_dates=spend["comparison_dates"], currency_code=currencies["spend"][(entity_id, report_date)])
        events.extend(detected)
    if _ready(mature_spend) and _ready(mature_orders) and mature_clicks is not None:
        basis = ("SEVEN_DAY" if mature_spend["comparison_basis"] == mature_orders["comparison_basis"] == "SEVEN_DAY"
                 else "PREVIOUS_DAY")
        detected = detect_campaign_anomalies(entity_key=entity_id, spend=mature_spend["current"],
            orders=mature_orders["current"], clicks=int(mature_clicks),
            prior_7_spend=_prior(values, entity_id, "spend", mature_day, mature_spend),
            prior_7_orders=_prior(values, entity_id, "orders", mature_day, mature_orders),
            daily_budget=None, config=config, campaign_enabled=campaign_enabled,
            high_spend_enabled=False, budget_enabled=False,
            previous_spend=mature_spend["previous_day"], previous_orders=mature_orders["previous_day"],
            comparison_basis=basis)
        dates = ([mature_day.isoformat(), *[(mature_day - timedelta(days=i)).isoformat() for i in range(1, 8)]]
                 if basis == "SEVEN_DAY" else [mature_day.isoformat(), (mature_day - timedelta(days=1)).isoformat()])
        for event in detected:
            event["metrics"].update(attribution_window_days=attribution_days,
                attribution_complete_report_date=mature_day.isoformat(), comparison_dates=dates,
                currency_code=currencies["spend"][(entity_id, mature_day)])
        events.extend(detected)
    complete = (_ready(spend) and _ready(mature_spend) and _ready(mature_orders)
                and clicks is not None and mature_clicks is not None)
    return events, complete, spend, mature_spend, mature_orders


def _ad_skipped_detail(values, entity, store_id, dimension, report_date, attribution_days):
    mature_day = report_date - timedelta(days=attribution_days)
    dates = sorted({report_date - timedelta(days=i) for i in range(8)} |
                   {mature_day - timedelta(days=i) for i in range(8)})
    required = {"spend": dates, "orders": [mature_day - timedelta(days=i) for i in range(8)],
                "clicks": [report_date, mature_day]}
    missing = [{"metric_name": metric, "metric_date": day.isoformat()} for metric, days in required.items()
               for day in days if (entity, day) not in values.get(metric, {})]
    return {"store_id": store_id, "dimension": dimension,
        "campaign_id" if dimension.startswith("campaign_") else "ad_id": entity,
        "reason": ("MISSING_CAMPAIGN_METRICS" if dimension.startswith("campaign_") else "MISSING_PRODUCT_AD_METRICS")
                  if missing else "CURRENCY_OR_OBSERVATION_UNCONFIRMED",
        "missing": missing}


def _evaluate_campaign_dimension(db, store_id: str, campaign_type: str,
                                 report_date: date, config: RuleConfig, *,
                                 skip_details: list[dict] | None = None
                                 ) -> tuple[list[AnomalyEvent], bool]:
    dimension = f"campaign_{campaign_type}"
    attribution_days = AD_ATTRIBUTION_DAYS[campaign_type]
    dates = [report_date - timedelta(days=i) for i in range(attribution_days + 8)]
    values, currencies, covered = _comparison_evidence(db, store_id, dimension, dates)
    campaigns = db.scalars(select(Campaign).where(Campaign.store_id == store_id,
        Campaign.campaign_type == campaign_type)).all()
    events, recovery_entities = [], set()
    complete = bool(campaigns)
    for campaign in campaigns:
        entity = campaign.campaign_id
        detected, known, spend, mature_spend, mature_orders = _ad_entity_events(
            values=values, currencies=currencies, covered=covered, entity_id=entity,
            report_date=report_date, attribution_days=attribution_days,
            campaign_enabled=(campaign.state or "").lower() == "enabled",
            daily_budget=campaign.budget if (campaign.budget_type or "").lower() == "daily" else None,
            config=config)
        if known:
            recovery_entities.add(str(entity))
        else:
            complete = False
            if skip_details is not None:
                skip_details.append(_ad_skipped_detail(values, entity, store_id, dimension,
                    report_date, attribution_days))
        if _ready(spend) and campaign.last_over_budget_at and str(campaign.last_over_budget_at).startswith(report_date.isoformat()):
            detected.append(_event("campaign_over_budget", "P1", entity, f"{entity}:campaign_over_budget",
                {"last_over_budget_at": campaign.last_over_budget_at, "daily_budget": campaign.budget,
                 "budget_type": campaign.budget_type, "report_date": report_date.isoformat()},
                "领星广告活动资料记录该活动在报告日发生过超预算事件"))
        events.extend(detected)
    saved = upsert_anomalies(db, store_id, events, evaluation_scope=f"{store_id}:campaign:{campaign_type}",
        scope_complete=complete, recovery_entity_ids=recovery_entities,
        escalation_after_days=config.escalate_after_days)
    return saved, complete


def _evaluate_product_ad_dimension(db, store_id: str, campaign_type: str,
                                   report_date: date, config: RuleConfig, *, skip_details=None
                                   ) -> tuple[list[AnomalyEvent], bool]:
    dimension = f"product_ad_{campaign_type}"
    attribution_days = AD_ATTRIBUTION_DAYS[campaign_type]
    dates = [report_date - timedelta(days=i) for i in range(attribution_days + 8)]
    values, currencies, covered = _comparison_evidence(db, store_id, dimension, dates)
    ads = db.scalars(select(AdProduct).where(AdProduct.store_id == store_id,
        AdProduct.campaign_type == campaign_type)).all()
    campaigns = {c.campaign_id: c for c in db.scalars(select(Campaign).where(
        Campaign.store_id == store_id, Campaign.campaign_type == campaign_type))}
    events, complete = [], bool(ads)
    for ad in ads:
        campaign = campaigns.get(ad.campaign_id)
        enabled = bool((ad.state or "").lower() == "enabled" and campaign
                       and (campaign.state or "").lower() == "enabled")
        detected, known, spend, mature_spend, mature_orders = _ad_entity_events(
            values=values, currencies=currencies, covered=covered, entity_id=ad.ad_id,
            report_date=report_date, attribution_days=attribution_days,
            campaign_enabled=enabled, daily_budget=None, config=config)
        if not known:
            complete = False
            if skip_details is not None:
                skip_details.append(_ad_skipped_detail(values, ad.ad_id, store_id, dimension,
                    report_date, attribution_days))
        for event in detected:
            event["entity_key"] = ad.seller_sku or ad.asin or ad.ad_id
            event["dedupe_key"] = f"{ad.ad_id}:{event['rule']}"
            event["metrics"].update(ad_id=ad.ad_id, campaign_id=ad.campaign_id,
                                     seller_sku=ad.seller_sku, asin=ad.asin)
        events.extend(detected)
    # Product display keys may be shared by multiple ads. Partial evidence must
    # never recover a sibling ad merely because its SKU matches a known one.
    saved = upsert_anomalies(db, store_id, events,
        evaluation_scope=f"{store_id}:product_ad:{campaign_type}", scope_complete=complete,
        escalation_after_days=config.escalate_after_days)
    return saved, complete


def evaluate_daily_anomalies(db, report_date: date | str | dict[str, str],
                             config: RuleConfig | None = None) -> dict:
    """Evaluate complete seven-day comparisons, falling back to an observed previous day.

    report_date may be a single date for legacy/single-timezone use, or a mapping
    of Lingxing sid to its own marketplace-local report date. A mapping restricts
    evaluation to the listed stores; dates are never inferred from another store.
    """
    if isinstance(report_date, dict):
        if not report_date:
            raise ValueError("report_dates mapping must not be empty")
        target_dates = {
            str(store_id): _parse_date(value)
            for store_id, value in report_date.items()
        }
        stores = db.scalars(
            select(Store).where(Store.sid.in_(list(target_dates)))
        ).all()
        found_ids = {store.sid for store in stores}
        missing_ids = set(target_dates) - found_ids
        if missing_ids:
            raise ValueError(
                "Report-date mapping contains unsynchronized store IDs: "
                + ", ".join(sorted(missing_ids))
            )
        normalized_dates = {
            store_id: target.isoformat() for store_id, target in target_dates.items()
        }
    else:
        target_date = _parse_date(report_date)
        stores = db.scalars(select(Store)).all()
        target_dates = {store.sid: target_date for store in stores}
        normalized_dates = target_date.isoformat()

    if config is None:
        from jx_alert.config import get_settings
        settings = get_settings()
        config = RuleConfig(
            escalate_after_days=settings.alert_escalate_after_days,
            order_change_ratio=settings.alert_order_change_ratio,
            high_spend_ratio=settings.alert_high_spend_ratio,
            low_spend_ratio=settings.alert_low_spend_ratio,
            budget_ratio=settings.alert_budget_ratio,
            min_spend=settings.alert_min_spend,
            min_clicks=settings.alert_min_clicks,
            min_orders_baseline=settings.alert_min_orders_baseline,
        )
    evaluated_scopes = 0
    skipped_scopes = 0
    skipped_scope_details: list[dict] = []
    saved_events: list[AnomalyEvent] = []

    for store in stores:
        store_id = store.sid
        if store.status != 1:
            skipped_scopes += 8
            skipped_scope_details.append({"store_id": store_id,
                "reason": "AUTHORIZATION_FAILED" if store.status == 2 else "STORE_NOT_ACTIVE"})
            continue
        target_date = target_dates[store_id]
        sku_msku = db.scalars(select(Product.seller_sku).where(
            Product.store_id == store_id
        )).all()
        sku_asin = db.scalars(select(Product.asin).where(
            Product.store_id == store_id,
            Product.asin.is_not(None),
        ).distinct()).all()

        for dimension, entities in (
            ("store", [store_id]),
            ("sku_msku", sorted(set(sku_msku))),
            ("sku_asin", sorted(set(sku_asin))),
        ):
            saved, complete = _evaluate_sales_dimension(
                db, store_id, dimension, entities, target_date, config,
                skip_details=skipped_scope_details,
            )
            saved_events.extend(saved)
            if complete:
                evaluated_scopes += 1
            else:
                skipped_scopes += 1

        for campaign_type in ("sp", "sb", "sd"):
            saved, complete = _evaluate_campaign_dimension(
                db, store_id, campaign_type, target_date, config,
                skip_details=skipped_scope_details,
            )
            # A partly known SP scope may still produce valid alerts for complete
            # campaigns; missing campaigns are excluded from recovery.
            saved_events.extend(saved)
            if complete:
                evaluated_scopes += 1
            else:
                skipped_scopes += 1

        for campaign_type in ("sp", "sd"):
            saved, complete = _evaluate_product_ad_dimension(
                db, store_id, campaign_type, target_date, config,
                skip_details=skipped_scope_details,
            )
            saved_events.extend(saved)
            if complete:
                evaluated_scopes += 1
            else:
                skipped_scopes += 1

    queued = enqueue_anomaly_notifications(db, saved_events)
    db.commit()
    return {
        "report_date": normalized_dates if isinstance(normalized_dates, str) else None,
        "report_dates": normalized_dates if isinstance(normalized_dates, dict) else None,
        "stores_seen": len(stores),
        "evaluated_scopes": evaluated_scopes,
        "skipped_incomplete_scopes": skipped_scopes,
        "skipped_scope_details": skipped_scope_details,
        "events_touched": len(saved_events),
        "notifications_queued": queued,
    }


def evaluate_store_status_events(
    db, stores_response: dict, escalation_after_days: int | None = None
) -> dict:
    """Create/recover store-status events directly from the authoritative seller list."""
    if escalation_after_days is None:
        from jx_alert.config import get_settings
        escalation_after_days = get_settings().alert_escalate_after_days
    records = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")

    saved_events = []
    non_normal = 0
    for record in records:
        sid = record.get("sid")
        if sid is None or record.get("status") is None:
            raise RuntimeError("Lingxing seller-list row must contain sid and status")
        store_id = str(sid)
        try:
            status_code = int(record["status"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid Lingxing store status for sid={store_id}") from exc

        events = []
        if status_code != 1:
            non_normal += 1
            status_labels = {
                0: "停止同步",
                2: "授权异常",
                3: "欠费停服",
            }
            status_label = status_labels.get(status_code, "未知店铺状态")
            severity = "P1" if status_code in {2, 3} else "P2"
            events.append(_event(
                "store_status_not_normal",
                severity,
                store_id,
                f"{store_id}:store_status_not_normal",
                {
                    "store_name": record.get("name"),
                    "status_code": status_code,
                    "status_label": status_label,
                    "has_ads_setting": record.get("has_ads_setting"),
                },
                f"店铺「{record.get('name') or store_id}」状态为「{status_label}」"
                f"（领星状态码 {status_code}），请检查领星店铺同步/授权状态",
            ))
        saved_events.extend(upsert_anomalies(
            db,
            store_id,
            events,
            evaluation_scope=f"{store_id}:store_status",
            scope_complete=True,
            escalation_after_days=escalation_after_days,
        ))

    queued = enqueue_anomaly_notifications(db, saved_events)
    db.commit()
    return {
        "stores_checked": len(records),
        "non_normal_stores": non_normal,
        "events_touched": len(saved_events),
        "notifications_queued": queued,
    }
