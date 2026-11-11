"""Daily store, ASIN and MSKU sales snapshots from Lingxing's documented sales API."""
from datetime import date, datetime, timezone

from sqlalchemy import delete, select

from jx_alert.storage.models import DailyMetric, Product, RawSnapshot, Store


SALES_TYPES = {1: "sales", 2: "units", 3: "orders"}


def _store_local_date(report_dates: str | dict[str, str], sid: str) -> str:
    value = report_dates if isinstance(report_dates, str) else report_dates.get(sid)
    if not isinstance(value, str):
        raise ValueError(f"Missing store-local report date for sid={sid}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid store-local report date for sid={sid}") from exc
    return value


def _fetch_sales_rows(client, payload: dict, *, page_size: int = 1000,
                      max_pages: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        result = client.sales_daily({**payload, "offset": offset, "length": page_size})
        page = result.get("data") if isinstance(result, dict) else None
        if not isinstance(page, list):
            raise RuntimeError("Lingxing ASIN/MSKU sales response data must be an array")
        rows.extend(page)
        raw_total = result.get("total")
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Lingxing sales response total is invalid") from exc
            if offset + len(page) >= total:
                return rows
            if not page:
                raise RuntimeError(
                    f"Lingxing sales pagination incomplete: {offset} of {total} rows returned"
                )
        elif len(page) < page_size:
            return rows

        if not page:
            return rows
        offset += len(page)

    raise RuntimeError(f"Lingxing sales report exceeded max_pages={max_pages}")


def _numeric_map(rows: list[dict], *, key_field: str, expected_date: str,
                 metric_name: str) -> dict[str, tuple[float, str | None, dict]]:
    values: dict[str, tuple[float, str | None, dict]] = {}
    for row in rows:
        key = row.get(key_field)
        if key is None or str(key).strip() == "":
            raise RuntimeError(f"Lingxing sales row is missing {key_field}")
        row_date = row.get("r_date")
        if row_date and row_date != expected_date:
            raise RuntimeError("Lingxing sales report returned an unexpected r_date")
        try:
            value = float(row["map_value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Lingxing sales row has invalid map_value for {metric_name}") from exc
        currency = row.get("currency_code")
        values[str(key)] = (value, currency, row)
    return values


def _upsert_store(db, record: dict) -> str:
    sid = record.get("sid")
    if sid is None:
        raise RuntimeError("Lingxing seller-list record is missing sid")
    store_id = str(sid)
    row = db.get(Store, store_id)
    if row is None:
        row = Store(sid=store_id, name=str(record.get("name") or store_id))
        db.add(row)
    row.name = str(record.get("name") or store_id)
    row.mid = str(record["mid"]) if record.get("mid") is not None else None
    row.seller_id = record.get("seller_id")
    row.account_name = record.get("account_name")
    row.region = record.get("region")
    row.country = record.get("country")
    row.marketplace_id = record.get("marketplace_id")
    row.status = int(record.get("status") or 0)
    row.has_ads_setting = int(record.get("has_ads_setting") or 0)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return store_id


def collect_sales_for_day(client, db, report_dates: str | dict[str, str], *,
                          stores_response: dict | None = None,
                          page_size: int = 1000) -> dict:
    """Collect complete ASIN/MSKU daily metrics; missing API values are never filled as zero.

    The official API requires three calls per dimension: type=1 sales, type=2 units,
    type=3 order count. The function only replaces the same store/date dimensions
    after every required page has been fetched successfully.
    """
    stores_response = stores_response or client.get_stores()
    stores = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(stores, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")

    db.add(RawSnapshot(source="lingxing_seller_list", payload=stores_response))
    active_stores = []
    for record in stores:
        store_id = _upsert_store(db, record)
        if int(record.get("status") or 0) == 1:
            active_stores.append((store_id, record))
    db.flush()

    report_count = 0
    product_count = 0
    skipped_stores = len(stores) - len(active_stores)
    for store_id, store in active_stores:
        sid = store["sid"]
        local_date = _store_local_date(report_dates, store_id)
        metric_date = date.fromisoformat(local_date)
        collected: dict[int, dict[str, dict[str, tuple[float, str | None, dict]]]] = {
            1: {}, 2: {}
        }
        raw_payloads: list[tuple[str, dict]] = []

        for asin_type, dimension, key_field in (
            (1, "sku_asin", "asin"),
            (2, "sku_msku", "seller_sku"),
        ):
            for api_type, metric_name in SALES_TYPES.items():
                payload = {
                    "sid": sid,
                    "event_date": local_date,
                    "asin_type": asin_type,
                    "type": api_type,
                }
                rows = _fetch_sales_rows(client, payload, page_size=page_size)
                raw_payloads.append((
                    f"lingxing_sales_{dimension}_{metric_name}",
                    {
                        "request": payload,
                        "rows": rows,
                    },
                ))
                collected[asin_type][metric_name] = _numeric_map(
                    rows, key_field=key_field, expected_date=local_date,
                    metric_name=metric_name,
                )

        # Validate the three complete responses before replacing existing metrics.
        for asin_type, dimension, key_field in (
            (1, "sku_asin", "asin"),
            (2, "sku_msku", "seller_sku"),
        ):
            keys = set().union(*(
                set(collected[asin_type][metric].keys()) for metric in SALES_TYPES.values()
            ))
            for key in keys:
                for metric_name in SALES_TYPES.values():
                    item = collected[asin_type][metric_name].get(key)
                    if item is None:
                        # An absent metric remains absent; do not invent a zero.
                        continue
                    _, _, source_row = item
                    db.add(RawSnapshot(
                        source=f"lingxing_sales_row_{dimension}",
                        store_id=store_id,
                        payload={"report_date": local_date, "row": source_row},
                    ))

        for source, payload in raw_payloads:
            db.add(RawSnapshot(source=source, store_id=store_id, payload=payload))

        # Upsert product identity from the MSKU response. Local SKU and owners are
        # intentionally preserved; they are operator-maintained mapping fields.
        msku_rows = collected[2]["sales"]
        for seller_sku, (_, currency, source_row) in msku_rows.items():
            product = db.scalar(select(Product).where(
                Product.store_id == store_id,
                Product.seller_sku == seller_sku,
            ))
            if product is None:
                product = Product(store_id=store_id, seller_sku=seller_sku)
                db.add(product)
            product.asin = str(source_row.get("asin")) if source_row.get("asin") else None
            product.product_name = source_row.get("product_name")
            product.currency_code = currency
            product.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            product_count += 1

        for asin_type, dimension, key_field in (
            (1, "sku_asin", "asin"),
            (2, "sku_msku", "seller_sku"),
        ):
            db.execute(delete(DailyMetric).where(
                DailyMetric.store_id == store_id,
                DailyMetric.metric_date == metric_date,
                DailyMetric.dimension == dimension,
            ))
            for metric_name, key_values in collected[asin_type].items():
                for entity_id, (value, currency, _) in key_values.items():
                    db.add(DailyMetric(
                        store_id=store_id,
                        metric_date=metric_date,
                        dimension=dimension,
                        entity_id=entity_id,
                        metric_name=metric_name,
                        value=value,
                        currency=currency,
                        source="lingxing_sales_report",
                    ))

        # Store totals are derived from the ASIN-grain result, never mixed with
        # MSKU-grain rows (which would double-count products with multiple MSKUs).
        db.execute(delete(DailyMetric).where(
            DailyMetric.store_id == store_id,
            DailyMetric.metric_date == metric_date,
            DailyMetric.dimension == "store",
        ))
        for metric_name, key_values in collected[1].items():
            if not key_values:
                continue
            currencies = {currency for _, currency, _ in key_values.values() if currency}
            if len(currencies) > 1:
                raise RuntimeError("One store/date sales report contains multiple currencies")
            db.add(DailyMetric(
                store_id=store_id,
                metric_date=metric_date,
                dimension="store",
                entity_id=store_id,
                metric_name=metric_name,
                value=sum(value for value, _, _ in key_values.values()),
                currency=next(iter(currencies), None),
                source="lingxing_sales_report",
            ))
        db.commit()
        report_count += sum(len(rows) for rows in collected[1].values()) + sum(
            len(rows) for rows in collected[2].values()
        )

    return {
        "stores_seen": len(stores),
        "active_stores_collected": len(active_stores),
        "stores_skipped_by_status": skipped_stores,
        "sales_rows": report_count,
        "products_seen": product_count,
    }
