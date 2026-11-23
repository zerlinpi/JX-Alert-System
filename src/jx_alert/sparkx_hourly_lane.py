"""Scheduler orchestration for isolated SparkX observations (never Feishu alerts)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import anyio
from sqlalchemy import select

from jx_alert.ad_source_controls import observation_source, source_snapshot
from jx_alert.operations import CollectionConfigurationError, _checked_scope
from jx_alert.sparkx_backend import (
    SparkXBackendError, _observation_scope_id, persist_sparkx_observation, read_sparkx_observations,
)
from jx_alert.storage.models import Store
from jx_alert.store_controls import collection_states


def selected_ad_provider(settings, sid):
    """Read the user's selection, even when SparkX verification no longer passes."""
    try:
        value = source_snapshot(settings, sid)["source"]
        return value if value in {"lingxing", "sparkx"} else None
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


def sparkx_hourly_pass(db, settings, *, slot, now, previous=None):
    """Read only explicit active shop scopes and append verified date/grain partitions."""
    previous = previous or {}
    try:
        allowed = _checked_scope(settings, None)
    except CollectionConfigurationError:
        return {}
    if not allowed:
        return {}
    switches = collection_states(settings, allowed)
    inventory = list(db.scalars(select(Store).where(Store.sid.in_(allowed), Store.status == 1)))
    records, work = {}, []
    for store in inventory:
        sid = str(store.sid)
        if not switches.get(sid) or selected_ad_provider(settings, sid) != "sparkx":
            continue
        try:
            choice = source_snapshot(settings, sid)
            if observation_source(settings, store) != "sparkx":
                records[sid] = {"provider": "sparkx", "status": "BLOCKED", "reason": "BINDING_NOT_VERIFIED"}
                continue
            marker = _observation_scope_id(store, choice)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            records[sid] = {"provider": "sparkx", "status": "BLOCKED", "reason": "CONFIGURATION_UNAVAILABLE"}
            continue
        if previous.get(sid, {}).get("status") == "OBSERVED" and previous[sid].get("selection_id") == marker:
            records[sid] = previous[sid]
            continue
        frozen = SimpleNamespace(**{key: getattr(store, key) for key in (
            "sid", "status", "country", "marketplace_id", "timezone", "currency_code")})
        work.append((sid, marker, choice, frozen))
    # Provider calls can take time. Do not keep a pre-fetch MariaDB snapshot.
    if work:
        db.rollback()

    def fetch(item):
        sid, marker, choice, frozen = item
        try:
            observed, failures = anyio.run(lambda: read_sparkx_observations(
                settings, frozen, choice["binding"], now=now))
        except SparkXBackendError as exc:
            observed, failures = {}, {"provider": exc.code}
        except Exception:
            observed, failures = {}, {"provider": "SPARKX_PROVIDER_UNAVAILABLE"}
        return sid, marker, observed, failures, now or datetime.now(timezone.utc)

    if work:
        with ThreadPoolExecutor(max_workers=min(len(work), 4)) as pool:
            for sid, marker, observed, errors, received in pool.map(fetch, work):
                try:
                    allowed = _checked_scope(settings, None)
                    fresh = db.get(Store, sid, populate_existing=True)
                    current = source_snapshot(settings, sid)
                    if (sid not in allowed or not collection_states(settings, [sid]).get(sid)
                            or fresh is None or observation_source(settings, fresh) != "sparkx"
                            or _observation_scope_id(fresh, current) != marker):
                        records[sid] = {"provider": "sparkx", "status": "BLOCKED", "reason": "SCOPE_CHANGED"}
                        continue
                except (CollectionConfigurationError, KeyError, OSError, RuntimeError, TypeError, ValueError):
                    records[sid] = {"provider": "sparkx", "status": "BLOCKED", "reason": "SCOPE_CHANGED"}
                    continue
                if not observed:
                    records[sid] = {"provider": "sparkx", "status": "PARTIAL", "errors": errors or {
                        "provider": "SPARKX_NO_PROVEN_OBSERVATION"}, "selection_id": marker}
                    db.rollback()
                    continue
                partitions = {gran: persist_sparkx_observation(db, fresh, current, payload,
                    observed_at=received, slot=slot) for gran, payload in observed.items()}
                db.commit()
                records[sid] = {"provider": "sparkx", "status": "PARTIAL" if errors else "OBSERVED",
                                "selection_id": marker, "observations": partitions, "errors": errors}
    return records
