"""Amazon Ads click-attribution lookback windows for seller campaigns."""

# Seller Sponsored Products uses a 7-day click lookback. Sponsored Brands and
# Sponsored Display use a 14-day lookback for the standard attributed metrics.
AD_ATTRIBUTION_DAYS = {
    "sp": 7,
    "sb": 14,
    "sd": 14,
}
