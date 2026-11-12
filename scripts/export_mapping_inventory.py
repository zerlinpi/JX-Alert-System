"""Export the authorized Lingxing seller list as an inactive manual mapping worksheet."""
import argparse
import csv
from pathlib import Path

from jx_alert.lingxing.client import LingxingClient


def safe_cell(value):
    value = str(value or "")
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.output)
    if path.exists():
        parser.error("Output exists; choose a new filename to preserve existing mappings")
    try:
        result = LingxingClient.from_settings().get_stores()
        stores = result.get("data")
        if not isinstance(stores, list):
            raise ValueError("Unexpected seller list")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(["sid", "store_name", "country", "status", "enable_after_review",
                             "verified_timezone", "verified_currency", "group_alias", "operator_open_ids"])
            for row in stores:
                writer.writerow([safe_cell(row.get(key)) for key in ['sid', 'name', 'country', 'status']]
                                + ["", "", "", "", ""])
        print(f"Exported {len(stores)} inactive mapping rows; no runtime scope was changed.")
    except Exception as exc:
        parser.exit(1, f"Mapping export failed: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
