"""Normalized numeric fields exposed by Lingxing advertising report endpoints."""

AD_REPORT_METRICS: dict[str, str] = {
    "cost": "spend",
    "orders": "orders",
    "sales": "sales",
    "units": "units",
    "clicks": "clicks",
    "impressions": "impressions",
    "same_orders": "same_orders",
    "same_sales": "same_sales",
    "same_units": "same_units",
    "view_impressions": "view_impressions",
    "vtr": "vtr",
    "vctr": "vctr",
    "new_to_brand_orders": "new_to_brand_orders",
    "new_to_brand_order_percentage": "new_to_brand_order_percentage",
    "new_to_brand_order_rate": "new_to_brand_order_rate",
    "new_to_brand_sales": "new_to_brand_sales",
    "new_to_brand_units": "new_to_brand_units",
    "attributed_orders_new_to_brand_14d": "attributed_orders_new_to_brand_14d",
    "attributed_sales_new_to_brand_14d": "attributed_sales_new_to_brand_14d",
    "attributed_units_ordered_new_to_brand_14d": "attributed_units_ordered_new_to_brand_14d",
}

# These are optional fields returned when show_detail=1. Keeping the windows
# separate avoids silently mixing 1/7/14/30-day attribution definitions.
for _base in ("orders", "sales", "units", "same_orders", "same_sales", "same_units"):
    for _window in ("1d", "7d", "14d", "30d"):
        AD_REPORT_METRICS[f"{_base}_{_window}"] = f"{_base}_{_window}"
