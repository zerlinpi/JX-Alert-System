"""Owner-selected SP observation source; do not rewrite warehouse or delivery facts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.sparkx_direct import SparkXDirectError, parse_profile_ids, validate_token
from jx_alert.store_controls import StoreControlError, _lock

_SID = re.compile(r"[1-9][0-9]{0,29}")
_MARKET = re.compile(r"[A-Z0-9]{5,30}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_MAX_FILE = 256 * 1024
_KEYS = {"profile_id", "marketplace", "currency", "timezone"}
_PROOF = {"verified_at", "token_fingerprint"}
_LINGXING = {"source": "lingxing", "binding": None, "verification": None, "updated_at": None, "selected_at": None}


class AdSourceError(RuntimeError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def _unavailable():
    return AdSourceError(503, "广告来源配置暂时不可用。现有配置不会被自动替换。")


def _conflict():
    return AdSourceError(409, "广告来源配置已变化，请刷新后重试。")


def _path(settings):
    return Path(getattr(settings, "advertising_source_controls_file", "/app/data/advertising-source-controls.json"))


def _json(path, *, limit=_MAX_FILE):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError
    return raw, json.loads(raw, object_pairs_hook=unique_json_fields)


def _binding(value, *, user=False):
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ValueError
    profile = value["profile_id"]
    if user and isinstance(profile, str) and re.fullmatch(r"[1-9][0-9]{0,18}", profile):
        profile = int(profile)
    market, currency, zone = value["marketplace"], value["currency"], value["timezone"]
    if (type(profile) is not int or not 0 < profile <= 2**63 - 1
            or not isinstance(market, str) or not _MARKET.fullmatch(market)
            or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
            or not isinstance(zone, str) or len(zone) > 64):
        raise ValueError
    ZoneInfo(zone)
    return {**value, "profile_id": profile}


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None or stamp.isoformat() != value:
        raise ValueError
    return stamp.astimezone(timezone.utc)


def _entry(value):
    if (not isinstance(value, dict) or set(value) != set(_LINGXING)
            or value["source"] not in ("lingxing", "sparkx")):
        raise ValueError
    updated_at = _timestamp(value["updated_at"])
    selected_at = value["selected_at"]
    if selected_at is not None and _timestamp(selected_at) > updated_at:
        raise ValueError
    if value["source"] == "sparkx" and selected_at is None:
        raise ValueError
    binding, verification = value["binding"], value["verification"]
    if binding is None:
        if value["source"] != "lingxing" or verification is not None:
            raise ValueError
    else:
        _binding(binding)
        if (not isinstance(verification, dict) or set(verification) != _PROOF
                or not _SHA256.fullmatch(verification["token_fingerprint"])):
            raise ValueError
        _timestamp(verification["verified_at"])


def _initialized_marker(path):
    return path.with_name(path.name + ".initialized")


def _snapshot(settings):
    path = _path(settings)
    marker = _initialized_marker(path)
    try:
        if path.is_symlink() or marker.is_symlink() or marker.exists() and not marker.is_file():
            raise ValueError
        if path.exists():
            raw, data = _json(path)
        elif marker.exists():
            # A lost previously initialized choice is not a deliberate rollback.
            raise ValueError
        else:
            raw, data = b"", {"version": 1, "stores": {}}
        if (not isinstance(data, dict) or set(data) != {"version", "stores"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["stores"], dict)):
            raise ValueError
        profiles = set()
        for sid, row in data["stores"].items():
            if not _SID.fullmatch(sid):
                raise ValueError
            _entry(row)
            if row["binding"] is not None:
                profile = row["binding"]["profile_id"]
                if profile in profiles:
                    raise ValueError
                profiles.add(profile)
        return data, hashlib.sha256(raw).hexdigest()
    except (OSError, TypeError, ValueError, RecursionError, KeyError):
        raise _unavailable() from None


def source_snapshot(settings, sid):
    """Internal immutable read; never expose verification fingerprints to the UI."""
    if not isinstance(sid, str) or not _SID.fullmatch(sid):
        raise AdSourceError(400, "店铺编号无效。")
    data, revision = _snapshot(settings)
    return {**data["stores"].get(sid, _LINGXING), "revision": revision}


def _token_basis(settings, profile):
    from jx_alert.sparkx_config import with_console_credentials

    settings = with_console_credentials(settings)
    try:
        token = settings.sparkx_ai_token.get_secret_value()
        validate_token(token)
        if profile not in parse_profile_ids(settings.sparkx_profile_ids):
            return None
        return hashlib.sha256(("jx-sparkx-observation-verifier:v1\0" + token).encode()).hexdigest()
    except (AttributeError, TypeError, SparkXDirectError, ValueError):
        return None


def _available_token(settings):
    from jx_alert.sparkx_config import with_console_credentials

    settings = with_console_credentials(settings)
    try:
        validate_token(settings.sparkx_ai_token.get_secret_value())
        return bool(parse_profile_ids(settings.sparkx_profile_ids))
    except (AttributeError, TypeError, ValueError, SparkXDirectError):
        return False


def _store_basis(settings, store, binding):
    if (store is None or store.status != 1 or store.marketplace_id != binding["marketplace"]
            or store.currency_code != binding["currency"] or store.timezone != binding["timezone"]):
        return False
    try:
        _, scope = _json(Path(settings.collection_scope_file))
        ids = scope.get("store_ids") if isinstance(scope, dict) else None
        if (not isinstance(ids, list) or not ids or any(type(sid) not in (str, int)
                or not _SID.fullmatch(str(sid)) for sid in ids)):
            return False
        unique_ids = set(map(str, ids))
        if len(unique_ids) != len(ids) or store.sid not in unique_ids:
            return False
        _, required = _json(Path(settings.store_timezones_file))
        stores = required.get("stores")
        zone = stores.get(store.sid) if isinstance(stores, dict) else None
        return (isinstance(zone, dict) and zone.get("timezone") == binding["timezone"]
                and zone.get("currency_code") == binding["currency"])
    except (AttributeError, OSError, TypeError, ValueError, KeyError, RecursionError):
        return False


def _verified(settings, store, current):
    binding, proof = current["binding"], current["verification"]
    if not binding or not proof or not _store_basis(settings, store, binding):
        return False
    basis = _token_basis(settings, binding["profile_id"])
    stamp = _timestamp(proof["verified_at"])
    return (basis is not None and stamp <= datetime.now(timezone.utc)
            and hmac.compare_digest(basis, proof["token_fingerprint"]))


@contextmanager
def source_commit_fence(settings):
    """Serialize a source-dependent durable commit with operating-console switches."""
    try:
        with _lock(_path(settings)):
            yield
    except StoreControlError as exc:
        raise _conflict() from exc
    except OSError:
        raise _unavailable() from None


def observation_source(settings, store, *, selection=None):
    """Return effective choice or None if its proof/configuration is unavailable.

    A selected but blocked SparkX source must not silently fall back to Lingxing.
    The caller still applies its existing store/collection eligibility checks.
    """
    try:
        current = source_snapshot(settings, store.sid) if selection is None else selection
        return current["source"] if current["source"] == "lingxing" or _verified(settings, store, current) else None
    except (AdSourceError, AttributeError, ValueError, TypeError, RecursionError):
        return None


def public_snapshot(settings, store, current=None):
    current = current or source_snapshot(settings, store.sid)
    binding = current["binding"]
    valid = _verified(settings, store, current)
    return {"source": current["source"], "revision": current["revision"], "updated_at": current["updated_at"],
            "selected_at": current["selected_at"],
            "observation_source": current["source"] if current["source"] == "lingxing" or valid else None,
            "binding": ({**binding, "profile_id": str(binding["profile_id"])} if binding else None),
            "verified_at": current["verification"]["verified_at"] if current["verification"] else None,
            "verification_status": "VERIFIED" if valid else "RECHECK_REQUIRED" if binding else "UNVERIFIED",
            "sparkx_ready": _available_token(settings),
            "store_basis": {"marketplace": store.marketplace_id, "currency": store.currency_code, "timezone": store.timezone}}


def _assert_revision(revision, current):
    if not isinstance(revision, str) or not _SHA256.fullmatch(revision):
        raise AdSourceError(400, "请求缺少有效配置版本。")
    if not hmac.compare_digest(revision, current["revision"]):
        raise _conflict()


def prepare_verification(settings, store, payload):
    """Check store identity and CAS before any explicit outbound read."""
    if set(payload) != {"revision", "binding"}:
        raise AdSourceError(400, "SparkX 绑定请求无效。")
    try:
        binding = _binding(payload["binding"], user=True)
    except (ValueError, TypeError, KeyError):
        raise AdSourceError(400, "请填写有效的 SparkX Profile、站点、币种和时区。") from None
    current = source_snapshot(settings, store.sid)
    _assert_revision(payload["revision"], current)
    if current["source"] == "sparkx" and binding != current["binding"]:
        raise AdSourceError(409, "请先切回领星，再更换 SparkX 绑定。")
    if not _store_basis(settings, store, binding):
        raise AdSourceError(409, "SparkX 绑定须与当前已授权店铺的站点、币种和时区一致。")
    proof = _token_basis(settings, binding["profile_id"])
    if proof is None:
        raise AdSourceError(409, "SparkX 服务端凭据或允许的 Profile 尚未配置。")
    data, revision = _snapshot(settings)
    if revision != current["revision"]:
        raise _conflict()
    if any(sid != store.sid and row["binding"] and row["binding"]["profile_id"] == binding["profile_id"]
           for sid, row in data["stores"].items()):
        raise AdSourceError(409, "此 SparkX Profile 已绑定其他店铺。")
    return binding, proof


def _write(settings, store, revision, update, *, authorize):
    path = _path(settings)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock(path):
            authorize()
            data, current = _snapshot(settings)
            old = {**data["stores"].get(store.sid, _LINGXING), "revision": current}
            _assert_revision(revision, old)
            row = update(old, data["stores"])
            if row is None:
                return public_snapshot(settings, store, old)
            data["stores"][store.sid] = row
            encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode()
            if len(encoded) > _MAX_FILE:
                raise _unavailable()
            fd, temporary = tempfile.mkstemp(prefix=".advertising-source-", dir=path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            marker = _initialized_marker(path)
            if row["selected_at"] is not None and not marker.exists():
                # A binding check does not switch the source. Create this only
                # before publishing an actual choice, and keep it after rollback.
                # A failed first verification then cannot stop existing alerts.
                marker_fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                    | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    os.fsync(marker_fd)
                finally:
                    os.close(marker_fd)
            os.replace(temporary, path)
            temporary = None
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return public_snapshot(settings, store, {**row, "revision": hashlib.sha256(encoded).hexdigest()})
    except StoreControlError as exc:
        raise _conflict() from exc
    except OSError:
        raise _unavailable() from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def save_verification(settings, store, payload, binding, proof, *, authorize):
    """Publish only a revalidated read-test receipt; never switch source implicitly."""
    def update(old, _stores):
        if prepare_verification(settings, store, payload) != (binding, proof):
            raise _conflict()
        now = datetime.now(timezone.utc).isoformat()
        return {"source": old["source"], "binding": binding,
                "verification": {"verified_at": now, "token_fingerprint": proof},
                "updated_at": now, "selected_at": old["selected_at"]}

    return _write(settings, store, payload["revision"], update, authorize=authorize)


def save_selection(settings, store, payload, *, authorize):
    if set(payload) != {"revision", "source"} or payload["source"] not in ("lingxing", "sparkx"):
        raise AdSourceError(400, "请选择领星或 SparkX。")

    def update(old, _stores):
        target = payload["source"]
        if target == "sparkx" and not _verified(settings, store, old):
            raise AdSourceError(409, "请先核验此店铺的 SparkX 只读范围。")
        if target == old["source"]:
            return None
        now = datetime.now(timezone.utc).isoformat()
        return {**{key: old[key] for key in _LINGXING}, "source": target,
                "updated_at": now, "selected_at": now}

    return _write(settings, store, payload["revision"], update, authorize=authorize)
