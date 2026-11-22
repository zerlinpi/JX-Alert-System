"""Persistent identity and conservative predecessor proofs for manual SparkX exports."""
from datetime import datetime
import hashlib
from itertools import product
import json
import re

from sqlalchemy import func, or_, select

from jx_alert.notifications.outbox import IN_FLIGHT, NotificationOutbox
from jx_alert.sparkx_direct import SparkXDirectError, page_size_for
from jx_alert.storage.models import NotificationLog

LEGACY_PREFIX = "sparkx-page:v1:"
EXPORT_PREFIX = "sparkx-page:v2:"
_HEX = r"[0-9a-f]{64}"
_META = "sparkx_export"
_UNSENT_STATES = {"SKIPPED", "EXPIRED", "DEAD"}
_BINDING_FIELDS = {"role", "receive_id", "receive_id_type", "open_ids", "routing_alias",
                   "routing_changed_at", "sparkx_profile_id", "sparkx_generation", "sparkx_currency"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _source_identity(query):
    identity = {**query, "page": query.get("page", 1), "pageSize": page_size_for(query)}
    del identity["userContext"]
    return identity


def _equivalent_identities(query):
    identity = _source_identity(query)
    if identity["factEntity"] == "keywordPlacement":
        return (identity,)  # AMS-only entity: SparkX requires omission.
    mode = (identity.pop("timeGranularity", "") or "daily").lower()
    # SparkX explicitly declares case-insensitivity and omitted/empty as
    # daily. Preserve the original outgoing request; canonicalize identity only.
    casings = ("".join(chars) for chars in product(*((c, c.upper()) for c in mode)))
    variants = tuple({**identity, "timeGranularity": variant} for variant in casings)
    return (identity, {**identity, "timeGranularity": ""}, *variants) if mode == "daily" else variants


def _legacy_key(identity, destination, store_id):
    return LEGACY_PREFIX + _digest([identity, destination, store_id])


def _family_key(identity, destination):
    return _digest([identity, destination["sparkx_profile_id"], destination["receive_id"]])


def export_keys(query, destination, store_id):
    # The actual recipient and upstream profile are the scope, not the binding
    # alias or generation. V1 detection still uses the precise old key formula.
    return (_family_key(_equivalent_identities(query)[0], destination),
            _legacy_key(_source_identity(query), destination, store_id))


def _export_key(family, predecessor):
    return f"{EXPORT_PREFIX}{family}:{predecessor or 0}"


def valid_export_key(key, payload):
    if not isinstance(payload, dict):
        return False
    if re.fullmatch(re.escape(LEGACY_PREFIX) + _HEX, key):
        return _META not in payload
    meta = payload.get(_META)
    if not isinstance(meta, dict) or meta.keys() != {"family", "previous_outbox_id"}:
        return False
    family, previous = meta["family"], meta["previous_outbox_id"]
    return (isinstance(family, str) and re.fullmatch(_HEX, family) is not None
            and (previous is None or type(previous) is int and 0 < previous < 2**63)
            and key == _export_key(family, previous))


def _destination(row):
    value = row.payload.get("delivery") if isinstance(row.payload, dict) else None
    return value if isinstance(value, dict) else {}


def _same_scope(row, destination):
    planned = _destination(row)
    return (row.chat_id == planned.get("receive_id") == destination["receive_id"]
            and planned.get("sparkx_profile_id") == destination["sparkx_profile_id"]
            and planned.get("role") == destination["role"]
            and planned.get("receive_id_type") == "chat_id")


def _verified_legacy_binding(row, destination):
    if not (_same_scope(row, destination) and valid_export_key(row.idempotency_key, row.payload)
            and row.payload.get("transport") == "app" and row.payload.get("fixed_route") is True
            and row.payload.get("open_ids") == []):
        return None
    binding = {k: v for k, v in _destination(row).items() if k != "sparkx_content_contract"}
    sid = row.payload.get("store_id")
    if (binding.keys() != _BINDING_FIELDS or not isinstance(sid, str)
            or not re.fullmatch("[1-9][0-9]{0,99}", sid) or binding["open_ids"] != []
            or not isinstance(binding["routing_alias"], str) or not binding["routing_alias"]
            or not isinstance(binding["sparkx_generation"], str)
            or not re.fullmatch("[0-9a-f]{32}", binding["sparkx_generation"])
            or not isinstance(binding["sparkx_currency"], str)
            or not re.fullmatch("[A-Z]{3}", binding["sparkx_currency"])):
        return None
    stamp = binding["routing_changed_at"]
    if stamp is not None:
        try:
            if datetime.fromisoformat(stamp).utcoffset() is None:
                return None
        except (ValueError, TypeError, OverflowError):
            return None
    return binding, sid


def _proof(db, row, content_contract):
    """Return positive evidence; a terminal label alone cannot authorize a new POST."""
    if row.status in IN_FLIGHT:
        return "ACTIVE"
    if row.claimed_at is not None or type(row.attempts) is not int or row.attempts < 0:
        return "UNVERIFIED"
    logs = db.execute(select(NotificationLog.channel, NotificationLog.status, NotificationLog.message_id)
                      .where(NotificationLog.dedupe_key == row.idempotency_key)
                      .order_by(NotificationLog.id)).all()
    if any(channel != "feishu_app" for channel, _, _ in logs):
        return "UNVERIFIED"
    intent = row.payload.get("delivery_intent")
    final_intent = (isinstance(intent, dict) and row.attempts > 0
                    and type(intent.get("attempt")) is int and intent["attempt"] == row.attempts
                    and intent.get("operation") == "create" and intent.get("destination") == row.chat_id
                    and intent.get("provider_uuid") == hashlib.sha256(row.idempotency_key.encode()).hexdigest()[:40]
                    and re.fullmatch(_HEX, str(intent.get("body_sha256", ""))) is not None)
    if row.status in _UNSENT_STATES and row.delivered_at is None:
        if row.attempts == 0 and intent is None and not logs:
            return "NEVER_POSTED"
        if _destination(row).get("sparkx_content_contract") == content_contract and (
                intent is None or row.status == "SKIPPED" and final_intent
                and intent.get("aborted_before_post") is True):
            if (logs and (logs[-1][1] == row.status or row.status == "EXPIRED" and logs[-1][1] == "PRE_SEND_RETRY")
                    and all(status == "PRE_SEND_RETRY" and msg is None for _, status, msg in logs[:-1])
                    and logs[-1][2] is None):
                return "NEVER_POSTED"
    if (row.status == "DELIVERED" and row.delivered_at and final_intent
            and "aborted_before_post" not in intent
            and logs and logs[-1][1] == "DELIVERED"
            and re.fullmatch(r"om_[A-Za-z0-9_-]{1,197}", logs[-1][2] or "") is not None
            and all(status == "PRE_SEND_RETRY" and msg is None for _, status, msg in logs[:-1])):
        return "VERIFIED_DELIVERED"
    return "UNVERIFIED"


class ExportHistory:
    """One logical page and actual recipient, across authorization revisions."""

    def __init__(self, db, query, destination, store_id, content_contract):
        self.db = db
        self.content_contract = content_contract
        identities = _equivalent_identities(query)
        self.family = _family_key(identities[0], destination)
        families = {_family_key(identity, destination) for identity in identities}
        exact = {_legacy_key(identity, destination, store_id) for identity in identities}
        self.rows = []
        self.block = None
        # V1 kept the frozen route and SID. Match historical aliases using
        # those exact values. Screen by group first: missing/noncanonical old
        # profile attributes must not cause suspect rows to go unseen.
        historical_keys = {}
        matching = {}
        older = db.scalars(select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key.startswith(LEGACY_PREFIX),
            or_(NotificationOutbox.chat_id == destination["receive_id"],
                NotificationOutbox.payload["delivery"]["receive_id"].as_string() == destination["receive_id"],
                NotificationOutbox.idempotency_key.in_(sorted(exact))),
        ).order_by(NotificationOutbox.id))
        for row in older:
            profile = _destination(row).get("sparkx_profile_id")
            valid_profile = (isinstance(profile, str) and re.fullmatch("[1-9][0-9]{0,18}", profile)
                             and int(profile) < 2**63)
            frozen = _verified_legacy_binding(row, {**destination, "sparkx_profile_id": profile}) if valid_profile else None
            if frozen is None or profile != destination["sparkx_profile_id"] and row.idempotency_key in exact:
                self.block = self.block or row
                continue
            if profile != destination["sparkx_profile_id"]:
                continue  # Valid old binding to a different upstream account.
            frozen_dest, sid = frozen
            cache_key = _digest([frozen_dest, sid])
            if cache_key not in historical_keys:
                historical_keys[cache_key] = {_legacy_key(identity, frozen_dest, sid) for identity in identities}
            if row.idempotency_key in historical_keys[cache_key]:
                matching[row.id] = row

        # Older V2 writers used raw granularity in the family hash. Keep the
        # only existing family/UUID if one matches, even if it's not canonical.
        # Multiple independently-created alias chains are never silently merged.
        family_column = func.substr(NotificationOutbox.idempotency_key, len(EXPORT_PREFIX) + 1, 64)
        aliases = {}
        for row, family in db.execute(select(NotificationOutbox, family_column).where(
                NotificationOutbox.idempotency_key.startswith(EXPORT_PREFIX),
                family_column.in_(sorted(families)),
        ).order_by(NotificationOutbox.id)):
            aliases.setdefault(family, []).append(row)
        revision_rows = []
        if aliases:
            self.family = max(aliases, key=lambda family: aliases[family][-1].id)
            revision_rows = aliases.pop(self.family)
            if aliases and self.block is None:
                self.block = next(iter(aliases.values()))[-1]
        if revision_rows:
            first = revision_rows[0]
            if not valid_export_key(first.idempotency_key, first.payload):
                raise SparkXDirectError("SparkX 导出历史无法验证，未入队")
            previous = first.payload[_META]["previous_outbox_id"]
            if previous is not None:
                original = matching.pop(previous, None)
                if original is None:
                    raise SparkXDirectError("SparkX 旧导出关联无法验证，未入队")
                self.rows.append(original)
            for row in revision_rows:
                prior_id = self.rows[-1].id if self.rows else None
                if (not valid_export_key(row.idempotency_key, row.payload)
                        or row.payload[_META]["previous_outbox_id"] != prior_id
                        or prior_id is not None and row.id <= prior_id or not _same_scope(row, destination)):
                    raise SparkXDirectError("SparkX 导出版本链无法验证，未入队")
                self.rows.append(row)
        elif matching:
            self.rows.append(matching.pop(max(matching)))
        # Several V1 rows can already exist after a generation rotation under
        # the old producer. Do not guess or create a branch off one of them.
        if matching and self.block is None:
            self.block = next(iter(matching.values()))

    @property
    def latest(self):
        return self.rows[-1] if self.rows else None

    def view(self):
        """Local diagnostic only: do not re-fetch, mutate records, or print the old report."""
        row = self.latest
        if not row:
            return {"status": "NO_EXPORT", "blocking_outbox_id": self.block.id if self.block else None}
        return {"status": row.status, "outbox_id": row.id, "revision": len(self.rows) - 1,
                "card_sha256": _digest(row.payload.get("card")),
                "predecessor_proof": _proof(self.db, row, self.content_contract),
                "blocking_outbox_id": self.block.id if self.block else None}

    def create_or_reuse(self, destination, store_id, card, *, fresh_after, ack_delivered):
        latest = self.latest
        if latest is not None and fresh_after is None:
            return latest
        if fresh_after is not None:
            for row in self.rows:
                if row.payload.get(_META, {}).get("previous_outbox_id") == fresh_after:
                    return row  # Same owner action, same revision even after an ambiguous receipt.
            if latest is None or latest.id != fresh_after:
                raise SparkXDirectError("新导出须指定同一查询页最近的 outbox 编号；请先检查状态")
        if self.block is not None:
            raise SparkXDirectError(f"旧导出 #{self.block.id} 缺少范围证据或存在多个历史版本，未入队；请人工核对")
        if latest is not None:
            for index, row in enumerate(self.rows):
                proof = _proof(self.db, row, self.content_contract)
                if proof == "VERIFIED_DELIVERED" and index == len(self.rows) - 1 and not ack_delivered:
                    raise SparkXDirectError("前一版已确认送达；新导出须显式加 --ack-delivered")
                if proof not in {"NEVER_POSTED", "VERIFIED_DELIVERED"}:
                    raise SparkXDirectError(f"前一导出 #{row.id} 活动中或缺少可靠回执/未发送证据，禁止创建新版")
        key = _export_key(self.family, fresh_after)
        row = NotificationOutbox(idempotency_key=key, chat_id=destination["receive_id"], payload={
            "text": "SparkX 广告表现（单页原生结果）", "store_id": store_id, "open_ids": [],
            "fixed_route": True, "transport": "app", "card": card,
            "delivery": {**destination, "sparkx_content_contract": self.content_contract},
            _META: {"family": self.family, "previous_outbox_id": fresh_after},
        })
        # The advisory lock serializes producers; the existing unique index
        # also prevents reuse if another producer ignored the lock. Do not use
        # a savepoint here: SQLite can release an initial savepoint as a commit.
        self.db.add(row)
        self.db.flush()
        return row
