"""Operator-supplied marketplace timezone and currency configuration."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.storage.models import Store


def load_store_config(path: str) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Store timezone file not found: {path}. Copy config/store-timezones.example.json "
            "to the configured path and fill every active Lingxing sid."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Store timezone file cannot be read or parsed") from exc

    stores = data.get("stores") if isinstance(data, dict) else None
    if not isinstance(stores, dict):
        raise RuntimeError("Store timezone config must contain a 'stores' object")

    normalized = {}
    for sid, config in stores.items():
        if not isinstance(config, dict) or not isinstance(config.get("timezone"), str):
            raise RuntimeError(f"Store {sid} must define an IANA timezone")
        try:
            ZoneInfo(config["timezone"])
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Unknown IANA timezone for store {sid}") from exc
        currency = config.get("currency_code", "")
        if currency and (
            not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha()
        ):
            raise RuntimeError(f"Store {sid} currency_code must be a 3-letter code")
        normalized[str(sid)] = {
            "timezone": config["timezone"],
            "currency_code": currency.upper() if currency else "",
        }
    return normalized


def build_store_report_dates(stores_response: dict, config_path: str, *,
                             now: datetime | None = None) -> dict[str, str]:
    records = stores_response.get("data") if isinstance(stores_response, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("Lingxing seller-list response data must be an array")
    config = load_store_config(config_path)
    report_dates = {}

    for store in records:
        if int(store.get("status") or 0) != 1:
            continue
        sid = store.get("sid")
        if sid is None:
            raise RuntimeError("Active Lingxing seller record is missing sid")
        sid = str(sid)
        store_config = config.get(sid)
        if not store_config:
            raise RuntimeError(f"Missing timezone configuration for active Lingxing sid={sid}")
        timezone = ZoneInfo(store_config["timezone"])
        local_now = now.astimezone(timezone) if now and now.tzinfo else (
            now.replace(tzinfo=timezone) if now else datetime.now(timezone)
        )
        report_dates[sid] = (local_now.date() - timedelta(days=1)).isoformat()

    return report_dates


def sync_store_config(db, config_path: str) -> int:
    config = load_store_config(config_path)
    stores = db.scalars(select(Store)).all()
    updated = 0
    for store in stores:
        item = config.get(store.sid)
        if not item:
            continue
        store.timezone = item["timezone"]
        store.currency_code = item["currency_code"] or None
        updated += 1
    db.flush()
    return updated
