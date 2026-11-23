"""Explicit same-local-day preview from stored evidence. No collection or delivery."""
from collections import defaultdict
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from jx_alert.lingxing.api_business import FINANCE, PRODUCT
from jx_alert.notifications.owner_daily import owner_daily_snapshot
from jx_alert.notifications.owner_daily_card import build_owner_daily_cards, normalize_owner_row
from jx_alert.storage.models import Store


def _metric_states(raw, sources):
    row = normalize_owner_row(raw)  # Count only values the card itself will display.

    def metric(dimension, key):
        proof = sources[dimension]
        if proof["state"] != "RECENT_CAPTURE":
            return proof["state"]
        if key == "return_rate":
            if not row.valid_unit_pair:
                return "MISSING_OR_INVALID_FIELD"
            if row.sold_units == 0:
                return "ZERO_DENOMINATOR"
        elif proof.get("verified_currency") is None:
            return "CURRENCY_OR_FACTS_UNVERIFIED"
        elif proof["verified_currency"] != row.currency:
            return "CURRENCY_CONFLICT"
        elif getattr(row, key) is None:
            return "MISSING_OR_INVALID_FIELD"
        return "PROVISIONAL_VALUE"

    return {key: metric(dimension, key) for dimension, key in (
        (PRODUCT, "ad_spend"), (PRODUCT, "sales"), (PRODUCT, "return_rate"), (FINANCE, "refund_amount")
    )}


def prepare_owner_today(db, scope_ids, *, now=None, as_of=None):
    """Caller supplies a verified scope and one consistent read-only DB snapshot."""
    if (not isinstance(scope_ids, (tuple, list)) or not scope_ids or
            any(type(sid) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,99}", str(sid))
                for sid in scope_ids)):
        raise ValueError("An explicit nonempty store scope is required")
    ids = sorted({str(sid) for sid in scope_ids})
    generated = now if now is not None else datetime.now(timezone.utc)
    if generated.utcoffset() is None:
        raise ValueError("Same-day preview requires a timezone-aware clock")
    generated = generated.astimezone(timezone.utc)
    if as_of is not None and (as_of.utcoffset() is None or as_of > generated):
        raise ValueError("Replay cutoff must be timezone-aware and not in the future")
    clock = generated if as_of is None else as_of.astimezone(timezone.utc)
    inventory = {store.sid: store for store in db.scalars(select(Store).where(Store.sid.in_(ids)))}
    grouped, blocked = defaultdict(list), []
    for sid in ids:
        store = inventory.get(sid)
        if not store or store.status != 1:
            blocked.append({"store_id": sid, "reason": "NO_ACTIVE_INVENTORY"})
            continue
        try:
            zone = ZoneInfo(store.timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            blocked.append({"store_id": sid, "reason": "TIMEZONE_UNVERIFIED"})
            continue
        grouped[clock.astimezone(zone).date(), zone.key].append(store)

    groups, observed_count = [], 0
    for (day, zone_name), stores in sorted(grouped.items()):
        rows, _, sources = owner_daily_snapshot(db, stores, day, now=clock)
        checks = [{"store_id": row["store_id"], "sources": sources[row["store_id"]],
                   "metrics": _metric_states(row, sources[row["store_id"]])} for row in rows]
        observed = sum(value == "PROVISIONAL_VALUE" for check in checks
                       for value in check["metrics"].values())
        observed_count += observed
        groups.append({"report_date": day.isoformat(), "timezone": zone_name, "stores": checks,
            "cards": build_owner_daily_cards(rows, report_date=day, as_of=clock, provisional=True,
                                            preview_scope=(zone_name, len(ids)), replay=as_of is not None)
                     if observed else []})
    return {
        "mode": "READ_ONLY_PREVIEW" if as_of is None else "READ_ONLY_REPLAY",
        "generated_at": generated.isoformat(), "scope_count": len(ids),
        **({"reference_cutoff_at": clock.isoformat()} if as_of is not None else {}),
        "provider_updated_at": None, "delivery_enabled": False,
        "note": ("店铺当地日尚未结束。完成采集不等于领星已更新至该时刻。只展示已存分区，不补采、不推送。"
                 if as_of is None else "按参考时刻核验所给副本；不证明副本在当时已封存或领星已更新至该时刻。不补采、不推送。"),
        "status": ("NO_PREVIEW_VALUES" if not observed_count else
                   "ALL_METRICS_PROVISIONAL" if observed_count == len(ids) * 4 else "PARTIAL_PREVIEW"),
        "blocked_stores": blocked, "groups": groups,
    }
