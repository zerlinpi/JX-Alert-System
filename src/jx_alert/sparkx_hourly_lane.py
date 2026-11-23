"""Scheduler orchestration for isolated SparkX observations (never Feishu alerts)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select

from jx_alert.ad_source_controls import AdSourceError, observation_source, source_commit_fence, source_snapshot
from jx_alert.operations import CollectionConfigurationError, _checked_scope
from jx_alert.sparkx_backend import (
    SOURCES, SparkXBackendError, _observation_scope_id, persist_sparkx_observation, read_sparkx_observations,
)
from jx_alert.storage.models import RawSnapshot, Store
from jx_alert.store_controls import collection_states


def selected_ad_provider(settings, sid):
    """Read the user's selection, even when SparkX verification no longer passes."""
    try:
        value = source_snapshot(settings, sid)["source"]
        return value if value in {"lingxing", "sparkx"} else None
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _current_selection(settings, store, marker):
    sid = str(store.sid)
    try:
        current = source_snapshot(settings, sid)
        allowed = (collection_states(settings, [sid]).get(sid)
                   and observation_source(settings, store, selection=current) == "sparkx"
                   and _observation_scope_id(store, current) == marker)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        allowed = False
    if not allowed:
        raise SparkXBackendError("SCOPE_CHANGED")
    return current


def _receipt(row):
    return {"snapshot_id": row.id, "row_count": row.payload["row_count"],
            "report_date": row.payload["report_date"], "state": "OBSERVED"}


def _checkpoint(db, sid, marker, slot, when, zone):
    """Resume from actual rows for this exact slot and date, not stale job metadata."""
    local = when.astimezone(ZoneInfo(zone)).date()
    dates = {"daily": (local - timedelta(days=1)).isoformat(), "hourly": local.isoformat()}
    result = {}
    for gran, source in SOURCES.items():
        existing = db.scalar(select(RawSnapshot).where(
            RawSnapshot.source == source, RawSnapshot.store_id == sid,
            RawSnapshot.payload["selection_id"].as_string() == marker,
            RawSnapshot.payload["monitor_slot"].as_string() == slot,
            RawSnapshot.payload["report_date"].as_string() == dates[gran],
        ).order_by(RawSnapshot.id.desc()).limit(1))
        if existing is not None:
            result[gran] = _receipt(existing)
    return result


def sparkx_hourly_pass(db, settings, *, slot, now):
    """Read only explicit active shop scopes and append verified date/grain partitions."""
    try:
        allowed = _checked_scope(settings, None)
    except CollectionConfigurationError:
        # A missing or unreadable scope cannot authorize SparkX, but looking
        # at local selections lets the hourly job report an explicit block.
        allowed = None
    candidates = select(Store).where(Store.status == 1)
    if allowed is not None:
        candidates = candidates.where(Store.sid.in_(allowed))
    inventory = list(db.scalars(candidates))
    switches = collection_states(settings, [str(row.sid) for row in inventory])
    records, work = {}, []
    for store in inventory:
        sid = str(store.sid)
        try:
            choice = source_snapshot(settings, sid)
            if choice["source"] != "sparkx":
                continue
            marker = _observation_scope_id(store, choice)
            entry = {"provider": "sparkx", "selection_id": marker}
            if not switches.get(sid):
                records[sid] = {**entry, "status": "PAUSED", "reason": "STORE_COLLECTION_PAUSED"}
                continue
            if allowed is None:
                records[sid] = {**entry, "status": "BLOCKED", "reason": "EXPLICIT_COLLECTION_SCOPE_REQUIRED"}
                continue
            if observation_source(settings, store, selection=choice) != "sparkx":
                records[sid] = {**entry, "status": "BLOCKED", "reason": "BINDING_NOT_VERIFIED"}
                continue
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            records[sid] = {"provider": "sparkx", "status": "BLOCKED", "reason": "CONFIGURATION_UNAVAILABLE"}
            continue
        frozen = SimpleNamespace(**{key: getattr(store, key) for key in (
            "sid", "status", "country", "marketplace_id", "timezone", "currency_code")})
        checkpoint = _checkpoint(db, sid, marker, slot, now or datetime.now(timezone.utc), frozen.timezone)
        if checkpoint.keys() == SOURCES.keys():
            records[sid] = {"provider": "sparkx", "status": "OBSERVED", "selection_id": marker,
                            "observations": checkpoint, "errors": {}}
            continue
        work.append((sid, marker, choice, frozen))
    # Provider calls can take time. Do not keep a pre-fetch MariaDB snapshot.
    if work:
        db.rollback()

    def fetch(item):
        sid, marker, choice, frozen = item

        def authorize():
            _current_selection(settings, frozen, marker)

        try:
            authorize()
            observed, failures = anyio.run(lambda: read_sparkx_observations(
                settings, frozen, choice["binding"], now=now, authorize=authorize))
        except SparkXBackendError as exc:
            observed, failures = {}, {"provider": exc.code}
        except Exception:
            observed, failures = {}, {"provider": "SPARKX_PROVIDER_UNAVAILABLE"}
        return sid, marker, observed, failures, now or datetime.now(timezone.utc)

    if work:
        with ThreadPoolExecutor(max_workers=min(len(work), 4)) as pool:
            for sid, marker, observed, errors, received in pool.map(fetch, work):
                # Restore a fresh MariaDB snapshot for each provider result,
                # even when the prior store was blocked and had nothing to write.
                db.rollback()
                try:
                    with source_commit_fence(settings):
                        fresh = db.get(Store, sid, populate_existing=True)
                        if fresh is None:
                            raise SparkXBackendError("SCOPE_CHANGED")
                        current = _current_selection(settings, fresh, marker)
                        if errors.get("provider") == "SCOPE_CHANGED":
                            raise SparkXBackendError("SCOPE_CHANGED")
                        partitions = {gran: persist_sparkx_observation(db, fresh, current, payload,
                            observed_at=received, slot=slot) for gran, payload in observed.items()}
                        db.commit()
                except AdSourceError as exc:
                    db.rollback()
                    if exc.status_code == 409:
                        records[sid] = {"provider": "sparkx", "status": "PARTIAL", "selection_id": marker,
                                        "errors": {"provider": "SOURCE_CONFIGURATION_BUSY"}}
                    else:
                        records[sid] = {"provider": "sparkx", "status": "BLOCKED", "selection_id": marker,
                                        "reason": "CONFIGURATION_UNAVAILABLE"}
                    continue
                except SparkXBackendError:
                    db.rollback()
                    records[sid] = {"provider": "sparkx", "status": "BLOCKED", "selection_id": marker,
                                    "reason": "SCOPE_CHANGED"}
                    continue
                if not partitions and not errors:
                    errors = {"provider": "SPARKX_NO_PROVEN_OBSERVATION"}
                records[sid] = {"provider": "sparkx", "status": "PARTIAL" if errors else "OBSERVED",
                                "selection_id": marker, "observations": partitions, "errors": errors}
    return records
