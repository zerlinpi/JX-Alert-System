"""Set the Feishu Open ID owners for one product in the local database."""
import argparse

from sqlalchemy import select

from jx_alert.storage.db import SessionLocal, init_db
from jx_alert.storage.models import Product


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-id", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--seller-sku")
    selector.add_argument("--asin")
    parser.add_argument("--open-ids", required=True,
                        help="Comma-separated Feishu Open IDs (ou_...).")
    args = parser.parse_args()

    open_ids = list(dict.fromkeys(
        item.strip() for item in args.open_ids.split(",") if item.strip()
    ))
    if not open_ids or any(not item.startswith("ou_") for item in open_ids):
        raise SystemExit("--open-ids must contain one or more valid Feishu Open IDs (ou_...).")

    init_db()
    with SessionLocal() as db:
        query = select(Product).where(Product.store_id == str(args.store_id))
        if args.seller_sku:
            query = query.where(Product.seller_sku == args.seller_sku)
        else:
            query = query.where(Product.asin == args.asin)
        products = db.scalars(query).all()
        if not products:
            raise SystemExit("No matching product found. Run a successful sales collection first.")
        for product in products:
            product.owner_open_ids = open_ids
        db.commit()
        print(f"Updated owner mapping for {len(products)} product row(s).")
        for product in products:
            print(
                f"store={product.store_id} seller_sku={product.seller_sku} "
                f"asin={product.asin} owner_count={len(product.owner_open_ids or [])}"
            )


if __name__ == "__main__":
    main()
