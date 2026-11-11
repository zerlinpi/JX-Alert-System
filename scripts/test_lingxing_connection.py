"""Dry-run by default. Add --request to make one read-only Lingxing store-list call."""
import argparse

from jx_alert.config import get_settings
from jx_alert.lingxing.client import LingxingClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--request",
        action="store_true",
        help="Make one real, read-only Lingxing store-list API request.",
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.lingxing_app_id or not settings.lingxing_app_secret:
        raise SystemExit("LINGXING_APP_ID and LINGXING_APP_SECRET must be configured locally.")
    if not args.request:
        print("DRY RUN: Lingxing credentials are present; no token or API request was made.")
        print("Run with --request only after token endpoint paths and response mappings are verified.")
        return

    client = LingxingClient.from_settings()
    result = client.get_stores()
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list):
        count = len(data)
    elif isinstance(data, dict):
        count = len(data.get("list", data.get("rows", []))) if isinstance(
            data.get("list", data.get("rows", [])), list
        ) else None
    else:
        count = None
    print("Lingxing read-only store-list request succeeded.")
    print("Response code:", result.get("code") if isinstance(result, dict) else "unknown")
    print("Store record count:", count if count is not None else "not inferred from response")


if __name__ == "__main__":
    main()
