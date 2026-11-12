"""Read-only SP/SD advertised-product daily reports.

The current public Lingxing documentation confirms SP and SD product-ad report
contracts. No SB product-ad report endpoint is wired until its exact contract is
confirmed; SB campaign-level reports remain available.
"""
from datetime import date, datetime, timezone

from sqlalchemy import delete, select

from jx_alert.lingxing.report_dates import normalize_store_report_dates
from jx_alert.lingxing.report_metrics import AD_REPORT_METRICS
from jx_alert.lingxing.sales_collector import _upsert_store
from jx_alert.storage.coverage import mark_coverage
from jx_alert.storage.models import AdProduct, DailyMetric, Product, RawSnapshot


AD_METRICS = AD_REPORT_METRICS


def _sync_product_ad_master_data(client, db, store_id: str, sid, report_type: str,
                                 page_size: int) -> int:
    """Synchronize current ad state before evaluating daily product-ad metrics."""
    rows = list(client.iter_advertising_product_ads(
        report_type, {"sid": sid}, length=page_size
    ))
    normalized = []
    seen_ids: set[str] = set()
    for record in rows:
        ad_id = record.get("ad_id")
        campaign_id = record.get("campaign_id")
        seller_sku = record.get("sku")
        asin = record.get("asin")
        state = record.get("state")
        if (
            ad_id is None or campaign_id is None
            or not isinstance(seller_sku, str) or not seller_sku.strip()
            or not isinstance(asin, str) or not asin.strip()
            or not isinstance(state, str) or not state.strip()
        ):
            raise RuntimeError(
                f"{report_type.upper()} product-ad master row is missing "
                "ad_id/campaign_id/sku/asin/state"
            )
        ad_id = str(ad_id)
        if ad_id in seen_ids:
            raise RuntimeError(
                f"{report_type.upper()} product-ad master list contains duplicate ad_id"
            )
        seen_ids.add(ad_id)
        normalized.append((
            record, ad_id, str(campaign_id), str(seller_sku), str(asin), state.lower()
        ))

    db.add(RawSnapshot(
        source=f"lingxing_{report_type}_product_ads",
        store_id=store_id,
        payload={"rows": rows},
    ))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for record, ad_id, campaign_id, seller_sku, asin, state in normalized:
        ad_product = db.scalar(select(AdProduct).where(
            AdProduct.store_id == store_id,
            AdProduct.campaign_type == report_type,
            AdProduct.ad_id == ad_id,
        ))
        if ad_product is None:
            ad_product = AdProduct(
                store_id=store_id,
                campaign_type=report_type,
                ad_id=ad_id,
                campaign_id=campaign_id,
            )
            db.add(ad_product)
        ad_product.campaign_id = campaign_id
        ad_product.ad_group_id = (
            str(record["ad_group_id"]) if record.get("ad_group_id") is not None else None
        )
        ad_product.asin = asin
        ad_product.seller_sku = seller_sku
        ad_product.state = state
        ad_product.serving_status = record.get("serving_status")
        ad_product.updated_at = now

        product = db.scalar(select(Product).where(
            Product.store_id == store_id,
            Product.seller_sku == seller_sku,
        ))
        if product is None:
            product = Product(store_id=store_id, seller_sku=seller_sku)
            db.add(product)
            # Production sessions disable autoflush. Several ads can share one
            # SKU, so make its identity visible before the next lookup.
            db.flush()
        product.asin = asin
        product.updated_at = now

    # A complete master-data response that no longer contains an ad must not
    # leave its previous "enabled" state active indefinitely.
    existing_ads = db.scalars(select(AdProduct).where(
        AdProduct.store_id == store_id,
        AdProduct.campaign_type == report_type,
    )).all()
    for ad_product in existing_ads:
        if ad_product.ad_id not in seen_ids:
            ad_product.state = "not_returned"
            ad_product.serving_status = "not_returned"
            ad_product.updated_at = now

    db.commit()
    return len(rows)


def collect_product_ads_for_day(client, db,
                                report_dates: str | dict[str, str | list[str]], *,
                                stores_response: dict | None = None,
                                page_size: int = 100) -> dict:
    stores_response = stores_response or client.get_stores()
    stores = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(stores, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")

    active_ad_stores = []
    for record in stores:
        store_id = _upsert_store(db, record)
        if int(record.get("status") or 0) == 1 and int(record.get("has_ads_setting") or 0) == 1:
            active_ad_stores.append((store_id, record))
    db.flush()

    row_counts = {"sp": 0, "sd": 0}
    master_counts = {"sp": 0, "sd": 0}
    for store_id, store in active_ad_stores:
        for report_type in ("sp", "sd"):
            master_counts[report_type] += _sync_product_ad_master_data(
                client, db, store_id, store["sid"], report_type, page_size
            )
        local_report_dates = normalize_store_report_dates(report_dates, store_id)
        for report_date in local_report_dates:
            metric_date = date.fromisoformat(report_date)
            for report_type in ("sp", "sd"):
                rows = list(client.iter_advertising_product_report(
                    report_type,
                    {
                        "sid": store["sid"],
                        "report_date": report_date,
                        "show_detail": 1,
                        "offset": 0,
                        "length": page_size,
                    },
                    length=page_size,
                ))

                # Validate the entire report before replacing prior rows for this date.
                normalized = []
                report_ad_ids = set()
                for record in rows:
                    if record.get("report_date") != report_date:
                        raise RuntimeError(
                            f"{report_type.upper()} product report returned an unexpected report_date"
                        )
                    ad_id = record.get("ad_id")
                    campaign_id = record.get("campaign_id")
                    seller_sku = record.get("sku")
                    asin = record.get("asin")
                    if ad_id is None or campaign_id is None or not seller_sku or not asin:
                        raise RuntimeError(
                            f"{report_type.upper()} product report row is missing ad_id/campaign_id/sku/asin"
                        )
                    if str(ad_id) in report_ad_ids:
                        raise RuntimeError("Product report contains duplicate ad_id for one date")
                    report_ad_ids.add(str(ad_id))
                    values = {}
                    for source_field, metric_name in AD_METRICS.items():
                        value = record.get(source_field)
                        if value is None:
                            if source_field in {
                                "cost", "orders", "sales", "units", "clicks", "impressions"
                            }:
                                raise RuntimeError(
                                    f"{report_type.upper()} product report row is missing "
                                    f"required metric {source_field}"
                                )
                            continue
                        try:
                            values[metric_name] = float(value)
                        except (TypeError, ValueError) as exc:
                            raise RuntimeError(
                                f"Invalid {source_field} in {report_type.upper()} product report"
                            ) from exc
                    normalized.append((record, str(ad_id), str(campaign_id), str(seller_sku),
                                       str(asin), values))

                db.add(RawSnapshot(
                    source=f"lingxing_{report_type}_product_ad_report",
                    store_id=store_id,
                    payload={"report_date": report_date, "rows": rows},
                ))
                dimension = f"product_ad_{report_type}"
                db.execute(delete(DailyMetric).where(
                    DailyMetric.store_id == store_id,
                    DailyMetric.metric_date == metric_date,
                    DailyMetric.dimension == dimension,
                ))

                for record, ad_id, campaign_id, seller_sku, asin, values in normalized:
                    product = db.scalar(select(Product).where(
                        Product.store_id == store_id,
                        Product.seller_sku == seller_sku,
                    ))
                    if product is None:
                        product = Product(store_id=store_id, seller_sku=seller_sku)
                        db.add(product)
                        db.flush()
                    product.asin = asin
                    product.product_name = product.product_name or record.get("product_name")
                    product.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

                    ad_product = db.scalar(select(AdProduct).where(
                        AdProduct.store_id == store_id,
                        AdProduct.campaign_type == report_type,
                        AdProduct.ad_id == ad_id,
                    ))
                    if ad_product is None:
                        ad_product = AdProduct(
                            store_id=store_id,
                            campaign_type=report_type,
                            ad_id=ad_id,
                            campaign_id=campaign_id,
                        )
                        db.add(ad_product)
                    ad_product.campaign_id = campaign_id
                    ad_product.ad_group_id = (
                        str(record["ad_group_id"]) if record.get("ad_group_id") is not None else None
                    )
                    ad_product.asin = asin
                    ad_product.seller_sku = seller_sku
                    ad_product.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

                    for metric_name, value in values.items():
                        db.add(DailyMetric(
                            store_id=store_id,
                            metric_date=metric_date,
                            dimension=dimension,
                            entity_id=ad_id,
                            metric_name=metric_name,
                            value=value,
                            currency=product.currency_code,
                            source=f"lingxing_{report_type}_product_ad_report",
                        ))

                mark_coverage(
                    db,
                    store_id=store_id,
                    metric_date=metric_date,
                    dimension=dimension,
                    row_count=len(rows),
                )
                db.commit()
                row_counts[report_type] += len(rows)

    return {
        "stores_seen": len(stores),
        "active_ad_stores": len(active_ad_stores),
        "master_ads": master_counts,
        "rows": row_counts,
    }
