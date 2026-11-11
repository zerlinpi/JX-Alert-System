"""Coverage-aware deterministic evaluation of daily sales and advertising metrics."""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from jx_alert.analysis.rules import RuleConfig, detect_campaign_anomalies, detect_order_anomaly, _event
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


def _has_complete_coverage(db, store_id: str, dimension: str,
                           dates: list[date]) -> bool:
    rows = db.scalars(select(DataCoverage).where(
        DataCoverage.store_id == store_id,
        DataCoverage.dimension == dimension,
        DataCoverage.metric_date.in_(dates),
        DataCoverage.status == "COMPLETE",
    )).all()
    return {row.metric_date for row in rows} >= set(dates)


def _metric_values(db, store_id: str, dimension: str, metric_name: str,
                   dates: list[date]) -> dict[tuple[str, date], float]:
    rows = db.scalars(select(DailyMetric).where(
        DailyMetric.store_id == store_id,
        DailyMetric.dimension == dimension,
        DailyMetric.metric_name == metric_name,
        DailyMetric.metric_date.in_(dates),
    )).all()
    return {(row.entity_id, row.metric_date): float(row.value) for row in rows}


def _evaluate_sales_dimension(db, store_id: str, dimension: str,
                              entities: list[str], report_date: date,
                              config: RuleConfig) -> tuple[list[AnomalyEvent], bool]:
    baseline_dates = [report_date - timedelta(days=offset) for offset in range(7, 0, -1)]
    previous_date = report_date - timedelta(days=1)
    required_dates = baseline_dates + [report_date]
    if not _has_complete_coverage(db, store_id, dimension, required_dates):
        return [], False

    events = []
    for metric_name in ("orders", "units", "sales"):
        values = _metric_values(db, store_id, dimension, metric_name, required_dates)
        for entity_id in entities:
            current = values.get((entity_id, report_date), 0.0)
            previous = values.get((entity_id, previous_date), 0.0)
            baseline = [values.get((entity_id, day), 0.0) for day in baseline_dates]
            events.extend(detect_order_anomaly(
                entity_key=entity_id,
                current_orders=current,
                previous_orders=previous,
                prior_7_days=baseline,
                config=config,
                metric_name=metric_name,
            ))

    scope = f"{store_id}:sales:{dimension}"
    saved = upsert_anomalies(
        db, store_id, events, evaluation_scope=scope, scope_complete=True
    )
    return saved, True


def _evaluate_campaign_dimension(db, store_id: str, campaign_type: str,
                                 report_date: date, config: RuleConfig
                                 ) -> tuple[list[AnomalyEvent], bool]:
    dimension = f"campaign_{campaign_type}"
    baseline_dates = [report_date - timedelta(days=offset) for offset in range(7, 0, -1)]
    previous_date = report_date - timedelta(days=1)
    required_dates = baseline_dates + [report_date]
    if not _has_complete_coverage(db, store_id, dimension, required_dates):
        return [], False

    campaigns = db.scalars(select(Campaign).where(
        Campaign.store_id == store_id,
        Campaign.campaign_type == campaign_type,
    )).all()
    metrics = {
        name: _metric_values(db, store_id, dimension, name, required_dates)
        for name in ("spend", "orders", "clicks")
    }
    events = []
    for campaign in campaigns:
        entity_id = campaign.campaign_id
        current_spend = metrics["spend"].get((entity_id, report_date), 0.0)
        previous_orders = metrics["orders"].get((entity_id, previous_date), 0.0)
        current_orders = metrics["orders"].get((entity_id, report_date), 0.0)
        prior_spend = [metrics["spend"].get((entity_id, day), 0.0) for day in baseline_dates]
        prior_orders = [metrics["orders"].get((entity_id, day), 0.0) for day in baseline_dates]
        current_clicks = int(metrics["clicks"].get((entity_id, report_date), 0.0))

        events.extend(detect_order_anomaly(
            entity_key=entity_id,
            current_orders=current_orders,
            previous_orders=previous_orders,
            prior_7_days=prior_orders,
            config=config,
            metric_name="orders",
        ))
        events.extend(detect_campaign_anomalies(
            entity_key=entity_id,
            spend=current_spend,
            orders=current_orders,
            clicks=current_clicks,
            prior_7_spend=prior_spend,
            prior_7_orders=prior_orders,
            daily_budget=(
                campaign.budget if (campaign.budget_type or "").lower() == "daily" else None
            ),
            config=config,
            campaign_enabled=(campaign.state or "").lower() == "enabled",
        ))
        if campaign.last_over_budget_at and str(campaign.last_over_budget_at).startswith(
            report_date.isoformat()
        ):
            from jx_alert.analysis.rules import _event
            events.append(_event(
                "campaign_over_budget",
                "P1",
                entity_id,
                f"{entity_id}:campaign_over_budget",
                {
                    "last_over_budget_at": campaign.last_over_budget_at,
                    "daily_budget": campaign.budget,
                    "budget_type": campaign.budget_type,
                },
                "领星广告活动资料记录该活动在报告日发生过超预算事件",
            ))

    scope = f"{store_id}:campaign:{campaign_type}"
    saved = upsert_anomalies(
        db, store_id, events, evaluation_scope=scope, scope_complete=True
    )
    return saved, True


def _evaluate_product_ad_dimension(db, store_id: str, campaign_type: str,
                                   report_date: date, config: RuleConfig
                                   ) -> tuple[list[AnomalyEvent], bool]:
    dimension = f"product_ad_{campaign_type}"
    baseline_dates = [report_date - timedelta(days=offset) for offset in range(7, 0, -1)]
    required_dates = baseline_dates + [report_date]
    if not _has_complete_coverage(db, store_id, dimension, required_dates):
        return [], False

    ads = db.scalars(select(AdProduct).where(
        AdProduct.store_id == store_id,
        AdProduct.campaign_type == campaign_type,
    )).all()
    metrics = {
        name: _metric_values(db, store_id, dimension, name, required_dates)
        for name in ("spend", "orders", "clicks")
    }
    campaigns = {
        row.campaign_id: row
        for row in db.scalars(select(Campaign).where(
            Campaign.store_id == store_id,
            Campaign.campaign_type == campaign_type,
        )).all()
    }
    events = []
    for ad in ads:
        ad_id = ad.ad_id
        current_spend = metrics["spend"].get((ad_id, report_date), 0.0)
        current_orders = metrics["orders"].get((ad_id, report_date), 0.0)
        current_clicks = int(metrics["clicks"].get((ad_id, report_date), 0.0))
        prior_spend = [metrics["spend"].get((ad_id, day), 0.0) for day in baseline_dates]
        prior_orders = [metrics["orders"].get((ad_id, day), 0.0) for day in baseline_dates]
        campaign = campaigns.get(ad.campaign_id)
        detected = detect_order_anomaly(
            entity_key=ad_id,
            current_orders=current_orders,
            previous_orders=metrics["orders"].get(
                (ad_id, report_date - timedelta(days=1)), 0.0
            ),
            prior_7_days=prior_orders,
            config=config,
            metric_name="orders",
        )
        detected.extend(detect_campaign_anomalies(
            entity_key=ad_id,
            spend=current_spend,
            orders=current_orders,
            clicks=current_clicks,
            prior_7_spend=prior_spend,
            prior_7_orders=prior_orders,
            daily_budget=None,
            config=config,
            campaign_enabled=bool(campaign and (campaign.state or "").lower() == "enabled"),
        ))
        for event in detected:
            event["entity_key"] = ad.seller_sku or ad.asin or ad_id
            event["dedupe_key"] = f"{ad_id}:{event['rule']}"
            event["metrics"]["ad_id"] = ad_id
            event["metrics"]["campaign_id"] = ad.campaign_id
            event["metrics"]["seller_sku"] = ad.seller_sku
            event["metrics"]["asin"] = ad.asin
        events.extend(detected)

    scope = f"{store_id}:product_ad:{campaign_type}"
    saved = upsert_anomalies(
        db, store_id, events, evaluation_scope=scope, scope_complete=True
    )
    return saved, True


def evaluate_daily_anomalies(db, report_date: date | str | dict[str, str],
                             config: RuleConfig | None = None) -> dict:
    """Evaluate only dimensions with complete coverage for each store's D-7 through D.

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
    saved_events: list[AnomalyEvent] = []

    for store in stores:
        store_id = store.sid
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
                db, store_id, dimension, entities, target_date, config
            )
            if complete:
                evaluated_scopes += 1
                saved_events.extend(saved)
            else:
                skipped_scopes += 1

        for campaign_type in ("sp", "sb", "sd"):
            saved, complete = _evaluate_campaign_dimension(
                db, store_id, campaign_type, target_date, config
            )
            if complete:
                evaluated_scopes += 1
                saved_events.extend(saved)
            else:
                skipped_scopes += 1

        for campaign_type in ("sp", "sd"):
            saved, complete = _evaluate_product_ad_dimension(
                db, store_id, campaign_type, target_date, config
            )
            if complete:
                evaluated_scopes += 1
                saved_events.extend(saved)
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
        "events_touched": len(saved_events),
        "notifications_queued": queued,
    }


def evaluate_store_status_events(db, stores_response: dict) -> dict:
    """Create/recover store-status events directly from the authoritative seller list."""
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
            severity = "P1" if status_code in {2, 3} else "P2"
            events.append(_event(
                "store_status_not_normal",
                severity,
                store_id,
                f"{store_id}:store_status_not_normal",
                {
                    "status_code": status_code,
                    "has_ads_setting": record.get("has_ads_setting"),
                },
                f"领星店铺列表返回非正常状态码：{status_code}，请检查店铺同步/授权状态",
            ))
        saved_events.extend(upsert_anomalies(
            db,
            store_id,
            events,
            evaluation_scope=f"{store_id}:store_status",
            scope_complete=True,
        ))

    queued = enqueue_anomaly_notifications(db, saved_events)
    db.commit()
    return {
        "stores_checked": len(records),
        "non_normal_stores": non_normal,
        "events_touched": len(saved_events),
        "notifications_queued": queued,
    }
