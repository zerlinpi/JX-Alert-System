"""Typed product identity for rule incidents, never inferred from a shared ASIN."""
from sqlalchemy import select

from jx_alert.storage.models import Product


def incident_seller_sku(store_id, entity_id, metrics, scope):
    if not isinstance(entity_id, str) or not entity_id.strip():
        return None
    if scope == f"{store_id}:sales:sku_msku":
        return entity_id
    # The product-ad evaluator copies these fields from its scoped AdProduct
    # row. Never treat a campaign ID or an ASIN-only product ad as a seller SKU.
    if (scope in (f"{store_id}:product_ad:sp", f"{store_id}:product_ad:sd")
            and metrics.get("seller_sku") == entity_id and metrics.get("ad_id")):
        return entity_id
    return None


def exact_store_product(db, store_id, seller_sku):
    rows = db.scalars(select(Product).where(
        Product.store_id == store_id, Product.seller_sku == seller_sku))
    # A warehouse collation may consider different strings equal. Accept only
    # the original store and MSKU; never retry by ASIN when the row is missing.
    return next((p for p in rows if p.store_id == store_id and p.seller_sku == seller_sku), None)


def ambiguous_legacy_product_key(db, store_id, seller_sku, *, scoped):
    """Legacy entity keys are untyped; ignore them if they also label another product."""
    stmt = select(Product.store_id, Product.seller_sku, Product.asin).where(Product.asin == seller_sku)
    if scoped:
        stmt = stmt.where(Product.store_id == store_id)
    return any(
        asin == seller_sku
        and (not scoped or candidate_store == store_id)
        and (candidate_store, candidate_sku) != (store_id, seller_sku)
        for candidate_store, candidate_sku, asin in db.execute(stmt)
    )
