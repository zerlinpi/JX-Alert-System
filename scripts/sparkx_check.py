"""Read-only SparkX check: confirm the configured token and list its authorized stores."""
from jx_alert.config import get_settings
from jx_alert.sparkx.credentials import check_connection


def main():
    result = check_connection(get_settings())
    if result["status"] != "OK":
        raise SystemExit(f"SparkX check failed: {result['status']} {result.get('message', '')}".strip())
    print(f"SparkX authorization verified: {result['profile_count']} profile(s).")
    for row in result["profiles"]:
        print(f"  {row['profile_id']}  {row['country']:>4}  {row['store_type'] or '-':>6}  {row['name']}")


if __name__ == "__main__":
    main()
