"""Offline acceptance for SparkX export families; does not call SparkX or Feishu."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from threading import Event

import pytest
from sqlalchemy import select
from test_sparkx_feishu import (
    ENVELOPE, PROFILE, QUERY, ROUTING, VENDOR_QUERY, _vendor_result,
    pilot as _source_pilot, result,
)

from jx_alert import sparkx_feishu as bridge
from jx_alert.notifications import worker
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.sparkx_export import (
    ExportHistory, export_keys, _family_key, _source_identity,
)
from jx_alert.sparkx_direct import SparkXDirectError
from jx_alert.storage.models import NotificationLog


@pytest.fixture(name="pilot")
def _isolated_pilot(tmp_path, monkeypatch):
    return _source_pilot.__wrapped__(tmp_path, monkeypatch)


def prepare(pilot, query=QUERY, **options):
    settings, session_factory, *_ = pilot
    return asyncio.run(bridge.prepare(settings, query, session_factory=session_factory, **options))


def refresh(pilot, prior_id, **options):
    return prepare(pilot, enqueue=True, fresh_after=prior_id, **options)


def expire(pilot, oid):
    _, sessions, _, _, posts, _ = pilot
    with sessions.begin() as db:
        db.get(NotificationOutbox, oid).created_at -= timedelta(minutes=31)
    assert worker.process_outbox_batch(outbox_ids=[oid])["sent"] == 0
    assert not posts


def legacy(pilot, oid, query=QUERY):
    settings, sessions, *_ = pilot
    with sessions.begin() as db:
        dest, sid = bridge._route(db, settings, PROFILE)
        _, v1_key = export_keys(query, dest, sid)
        row = db.get(NotificationOutbox, oid)
        row.idempotency_key = v1_key
        row.payload = {key: value for key, value in row.payload.items() if key != "sparkx_export"}
    return v1_key


def rotate_binding(pilot):
    _, _, path, *_ = pilot
    route = deepcopy(ROUTING)
    route["sparkx_profile_routes"][str(PROFILE)]["generation"] = "b" * 32
    path.write_text(json.dumps(route), encoding="utf-8")


def test_explicit_revision_refreshes_expired_card_without_rewriting_original(pilot):
    _, sessions, _, provider, posts, calls = pilot
    old = prepare(pilot, enqueue=True)
    expire(pilot, old["outbox_id"])
    with sessions() as db:
        original = deepcopy(db.get(NotificationOutbox, old["outbox_id"]).payload)
    before = len(calls)
    check = prepare(pilot, inspect=True)
    assert len(calls) == before and not posts
    assert (check["revision"], check["status"], check["predecessor_proof"]) == (0, "SKIPPED", "NEVER_POSTED")
    assert "card" not in check and len(check["card_sha256"]) == 64
    provider["page"] = result({**ENVELOPE, "data": {"rows": [{"Spend": 987}], "rowCount": 1}})
    assert prepare(pilot, enqueue=True) == {**old, "status": "SKIPPED"}
    new = refresh(pilot, old["outbox_id"])
    assert new["status"] == "PENDING" and new != old
    assert not posts  # the producer never calls Feishu
    assert refresh(pilot, old["outbox_id"]) == prepare(pilot, enqueue=True) == new
    assert prepare(pilot, inspect=True)["revision"] == 1
    assert worker.process_outbox_batch(outbox_ids=[new["outbox_id"]])["sent"] == 1
    assert len(posts) == 1 and '"987"' in posts[0]["content"]
    with sessions() as db:
        old_row, new_row = (db.get(NotificationOutbox, oid) for oid in (old["outbox_id"], new["outbox_id"]))
        assert old_row.payload == original and old_row.status == "SKIPPED"
        assert new_row.payload["sparkx_export"]["previous_outbox_id"] == old_row.id
        assert list(db.scalars(select(NotificationLog.status).order_by(NotificationLog.id))) == ["SKIPPED", "DELIVERED"]


def test_vendor_sourcing_fresh_export_requires_consistent_basis_even_after_verified_delivery(pilot):
    _, sessions, _, provider, posts, _ = pilot
    query = {**VENDOR_QUERY, "filters": {"distributorView_": "SOURCING"}}
    row = {"asin": "B000000001", "distributorView": "SOURCING", "ShippedRevenue": 17, "OrderedRevenue": 0}

    def set_source(**values):
        provider["page"] = result(_vendor_result(defaults={"sellingProgram": "RETAIL"}, rows=[{**row, **values}]))

    set_source()
    first = prepare(pilot, query, enqueue=True)
    old_id = first["outbox_id"]
    assert worker.process_outbox_batch(outbox_ids=[old_id])["sent"] == 1
    with sessions() as db:
        original = deepcopy(db.get(NotificationOutbox, old_id).payload)

    set_source(OrderedRevenue=42)
    with pytest.raises(SparkXDirectError, match="SOURCING 行返回与口径冲突"):
        prepare(pilot, query, enqueue=True, fresh_after=old_id, ack_delivered=True)
    with sessions() as db:
        assert list(db.scalars(select(NotificationOutbox.id))) == [old_id]
    assert len(posts) == 1

    set_source(ShippedRevenue=21)
    second = prepare(pilot, {**query, "timeGranularity": "DAILY"}, enqueue=True,
                     fresh_after=old_id, ack_delivered=True)
    assert worker.process_outbox_batch(outbox_ids=[second["outbox_id"]])["sent"] == 1
    assert len(posts) == 2 and posts[0]["uuid"] != posts[1]["uuid"]
    cards = [json.loads(message["content"]) for message in posts]
    assert [card["body"]["elements"][1]["rows"][0]["c2"] for card in cards] == ["17", "21"]
    assert all("不代表真实订单为零" in json.dumps(card, ensure_ascii=False) for card in cards)
    with sessions() as db:
        assert db.get(NotificationOutbox, old_id).payload == original


def test_same_page_after_verified_delivery_requires_explicit_deliberate_new_snapshot(pilot):
    _, sessions, _, _, posts, _ = pilot
    first = prepare(pilot, enqueue=True)
    assert worker.process_outbox_batch()["sent"] == 1
    assert prepare(pilot, inspect=True)["predecessor_proof"] == "VERIFIED_DELIVERED"
    assert prepare(pilot, enqueue=True) == {**first, "status": "DELIVERED"}
    with pytest.raises(SparkXDirectError, match="ack-delivered"):
        refresh(pilot, first["outbox_id"])
    second = refresh(pilot, first["outbox_id"], ack_delivered=True)
    assert second["outbox_id"] != first["outbox_id"] and len(posts) == 1
    assert worker.process_outbox_batch()["sent"] == 1
    assert len(posts) == 2 and posts[0]["uuid"] != posts[1]["uuid"]
    third = refresh(pilot, second["outbox_id"], ack_delivered=True)
    assert refresh(pilot, first["outbox_id"], ack_delivered=True) == {**second, "status": "DELIVERED"}
    assert prepare(pilot, enqueue=True) == third
    with sessions() as db:
        assert sorted(db.scalars(select(NotificationOutbox.id))) == sorted(
            x["outbox_id"] for x in (first, second, third))


def test_terminal_label_cannot_fake_receipt_or_confirm_old_unknown(pilot):
    _, sessions, _, provider, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    provider["lose_receipt"] = True
    assert worker.process_outbox_batch()["unknown"] == 1
    rotate_binding(pilot)
    assert prepare(pilot, enqueue=True) == {**row, "status": "DELIVERY_UNKNOWN"}
    with pytest.raises(SparkXDirectError, match="禁止创建新版"):
        refresh(pilot, row["outbox_id"], ack_delivered=True)
    with sessions.begin() as db:
        previous = db.get(NotificationOutbox, row["outbox_id"])
        previous.status, previous.delivered_at = "DELIVERED", previous.created_at
    # Even a terminal status plus matching persisted intent is not a receipt.
    with pytest.raises(SparkXDirectError, match="禁止创建新版"):
        refresh(pilot, row["outbox_id"], ack_delivered=True)
    assert len(posts) == 1


@pytest.mark.parametrize("state", ["PENDING", "RETRY", "PRE_SEND_RETRY", "SENDING", "SUBMITTING",
                                   "PREPARING", "DEAD", "REJECTED"])
def test_active_or_unverified_legacy_states_cannot_be_overridden(pilot, state):
    _, sessions, _, _, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    legacy(pilot, row["outbox_id"])
    with sessions.begin() as db:
        old = db.get(NotificationOutbox, row["outbox_id"])
        old.status = state
        old.attempts = 2
    with pytest.raises(SparkXDirectError, match="禁止创建新版"):
        refresh(pilot, row["outbox_id"], ack_delivered=True)
    assert prepare(pilot, enqueue=True) == {**row, "status": state} and not posts


def test_exact_legacy_row_keeps_original_uuid_and_links_to_new_family(pilot):
    _, sessions, _, _, posts, _ = pilot
    old = prepare(pilot, enqueue=True)
    key = legacy(pilot, old["outbox_id"])
    assert prepare(pilot, enqueue=True) == old
    assert worker.process_outbox_batch()["sent"] == 1
    assert posts[0]["uuid"] == hashlib.sha256(key.encode()).hexdigest()[:40]
    new = refresh(pilot, old["outbox_id"], ack_delivered=True)
    rotate_binding(pilot)
    assert prepare(pilot, enqueue=True) == new and len(posts) == 1
    with sessions() as db:
        assert db.get(NotificationOutbox, old["outbox_id"]).idempotency_key == key
        assert db.get(NotificationOutbox, new["outbox_id"]).payload["sparkx_export"]["previous_outbox_id"] == old["outbox_id"]


def test_previous_generation_unmapped_v1_is_not_an_escape_hatch(pilot):
    _, sessions, _, _, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    legacy(pilot, row["outbox_id"])
    with sessions.begin() as db:
        db.get(NotificationOutbox, row["outbox_id"]).status = "SENDING"
    rotate_binding(pilot)
    check = prepare(pilot, inspect=True)
    assert (check["status"], check["outbox_id"], check["predecessor_proof"]) == ("SENDING", row["outbox_id"], "ACTIVE")
    assert check["blocking_outbox_id"] is None
    assert prepare(pilot, enqueue=True) == {**row, "status": "SENDING"}
    with pytest.raises(SparkXDirectError, match="禁止创建新版"):
        refresh(pilot, row["outbox_id"], ack_delivered=True)
    assert not posts


def test_caller_cannot_use_predecessor_from_different_query_or_implicitly_enqueue(pilot):
    _, sessions, _, provider, posts, calls = pilot
    first = prepare(pilot, enqueue=True)
    before = list(calls)
    for options in ({"fresh_after": first["outbox_id"]}, {"enqueue": True, "fresh_after": True},
                    {"enqueue": True, "ack_delivered": True}, {"enqueue": True, "inspect": True}):
        with pytest.raises(SparkXDirectError):
            prepare(pilot, **options)
    assert calls == before
    provider["page"] = result({**ENVELOPE, "meta": {**ENVELOPE["meta"], "page": 2}})
    with pytest.raises(SparkXDirectError, match="同一查询页"):
        prepare(pilot, {**QUERY, "page": 2}, enqueue=True, fresh_after=first["outbox_id"])
    assert not posts
    with sessions() as db:
        assert list(db.scalars(select(NotificationOutbox.id))) == [first["outbox_id"]]


def test_simultaneous_fresh_operators_cannot_branch_or_overwrite(pilot, monkeypatch):
    _, sessions, _, _, posts, _ = pilot
    first = prepare(pilot, enqueue=True)
    expire(pilot, first["outbox_id"])
    held, release = Event(), Event()
    real = ExportHistory.create_or_reuse

    def pause(self, *args, **kwargs):
        if not held.is_set():
            held.set()
            if not release.wait(5):
                raise AssertionError("contending publisher did not complete")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ExportHistory, "create_or_reuse", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(refresh, pilot, first["outbox_id"])
        try:
            assert held.wait(5)
            with pytest.raises(SparkXDirectError, match="正在处理"):
                refresh(pilot, first["outbox_id"])
        finally:
            release.set()
        second = pending.result(timeout=5)
    assert refresh(pilot, first["outbox_id"]) == second
    assert not posts
    with sessions() as db:
        assert list(db.scalars(select(NotificationOutbox.id).order_by(NotificationOutbox.id))) == [
            first["outbox_id"], second["outbox_id"]]


def test_opaque_legacy_binding_fails_closed_even_if_scope_was_rotated(pilot):
    _, sessions, _, _, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    legacy(pilot, row["outbox_id"])
    with sessions.begin() as db:
        old = db.get(NotificationOutbox, row["outbox_id"])
        route = {**old.payload["delivery"], "sparkx_generation": False}
        old.payload = {**old.payload, "delivery": route}
        old.status = "DELIVERY_UNKNOWN"
    rotate_binding(pilot)
    assert prepare(pilot, inspect=True) == {"status": "NO_EXPORT", "blocking_outbox_id": row["outbox_id"]}
    with pytest.raises(SparkXDirectError, match="缺少范围证据"):
        prepare(pilot, enqueue=True)
    assert not posts


def test_rotated_legacy_unrelated_query_does_not_poison_this_page(pilot):
    _, _, _, provider, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    legacy(pilot, row["outbox_id"])
    rotate_binding(pilot)
    provider["page"] = result({**ENVELOPE, "meta": {**ENVELOPE["meta"], "page": 2}})
    assert prepare(pilot, {**QUERY, "page": 2}, inspect=True) == {
        "status": "NO_EXPORT", "blocking_outbox_id": None}
    second_page = prepare(pilot, {**QUERY, "page": 2}, enqueue=True)
    assert second_page["outbox_id"] != row["outbox_id"] and not posts


def test_multiple_legacy_rows_same_page_across_bindings_are_not_joined_or_replayed(pilot):
    settings, sessions, _, _, posts, _ = pilot
    first = prepare(pilot, enqueue=True)
    legacy(pilot, first["outbox_id"])
    expire(pilot, first["outbox_id"])
    rotate_binding(pilot)
    with sessions.begin() as db:
        old = db.get(NotificationOutbox, first["outbox_id"])
        destination, sid = bridge._route(db, settings, PROFILE)
        _, legacy_key = export_keys(QUERY, destination, sid)
        # Recreate a queue left behind by an older generation-sensitive producer.
        second = NotificationOutbox(idempotency_key=legacy_key, chat_id=old.chat_id,
            status="EXPIRED", payload={**old.payload, "delivery": {
                **destination, "sparkx_content_contract": bridge.CONTENT_CONTRACT}})
        db.add(second)
        db.flush()
        second_id = second.id
    info = prepare(pilot, inspect=True)
    assert info["outbox_id"] == second_id and info["blocking_outbox_id"] == first["outbox_id"]
    with pytest.raises(SparkXDirectError, match="多个历史版本"):
        refresh(pilot, second_id)
    assert not posts


@pytest.mark.parametrize("mode", ["", "daily", "DaIlY"])
def test_equivalent_daily_spelling_cannot_replay_a_lost_receipt(pilot, mode):
    _, _, _, provider, posts, _ = pilot
    first = prepare(pilot, enqueue=True)
    provider["lose_receipt"] = True
    assert worker.process_outbox_batch()["unknown"] == 1
    provider["lose_receipt"] = False
    alias = {**QUERY, "timeGranularity": mode}
    assert prepare(pilot, alias, enqueue=True) == {**first, "status": "DELIVERY_UNKNOWN"}
    assert prepare(pilot, alias, inspect=True)["outbox_id"] == first["outbox_id"]
    with pytest.raises(SparkXDirectError, match="禁止创建新版"):
        prepare(pilot, alias, enqueue=True, fresh_after=first["outbox_id"], ack_delivered=True)
    assert worker.process_outbox_batch()["sent"] == 0 and len(posts) == 1


@pytest.mark.parametrize("profile,wrong_column", [(None, False), ("--bad", False), (None, True)])
def test_missing_or_malformed_legacy_profile_cannot_hide_an_unknown(pilot, profile, wrong_column):
    _, sessions, _, provider, posts, _ = pilot
    first = prepare(pilot, enqueue=True)
    legacy(pilot, first["outbox_id"])
    provider["lose_receipt"] = True
    assert worker.process_outbox_batch()["unknown"] == 1
    with sessions.begin() as db:
        old = db.get(NotificationOutbox, first["outbox_id"])
        route = dict(old.payload["delivery"])
        if profile is None:
            del route["sparkx_profile_id"]
        else:
            route["sparkx_profile_id"] = profile
        if wrong_column:
            old.chat_id = "oc_bad_column"  # Frozen destination still links it to oc_test.
        old.payload = {**old.payload, "delivery": route}
    rotate_binding(pilot)
    assert prepare(pilot, inspect=True) == {"status": "NO_EXPORT", "blocking_outbox_id": first["outbox_id"]}
    with pytest.raises(SparkXDirectError, match="缺少范围证据"):
        prepare(pilot, enqueue=True)
    assert worker.process_outbox_batch()["sent"] == 0 and len(posts) == 1


def test_unrelated_verified_profile_does_not_poison_chat_history(pilot):
    _, sessions, _, _, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    legacy(pilot, row["outbox_id"])
    with sessions.begin() as db:
        old = db.get(NotificationOutbox, row["outbox_id"])
        destination = {k: v for k, v in old.payload["delivery"].items() if k != "sparkx_content_contract"}
        destination["sparkx_profile_id"] = str(PROFILE - 1)
        _, old.idempotency_key = export_keys({**QUERY, "profileIds": [PROFILE - 1]}, destination, "101")
        old.payload = {**old.payload, "delivery": {**destination, "sparkx_content_contract": bridge.CONTENT_CONTRACT}}
        old.status = "DELIVERY_UNKNOWN"
    assert prepare(pilot, inspect=True) == {"status": "NO_EXPORT", "blocking_outbox_id": None}
    assert prepare(pilot, enqueue=True)["outbox_id"] != row["outbox_id"] and not posts


def test_legacy_daily_alias_matches_after_binding_rotation_and_cannot_replay(pilot):
    _, _, _, provider, posts, _ = pilot
    historical = {**QUERY, "timeGranularity": "dAiLy"}
    row = prepare(pilot, historical, enqueue=True)
    legacy(pilot, row["outbox_id"], historical)
    provider["lose_receipt"] = True
    assert worker.process_outbox_batch()["unknown"] == 1
    rotate_binding(pilot)
    assert prepare(pilot, enqueue=True) == {**row, "status": "DELIVERY_UNKNOWN"}
    with pytest.raises(SparkXDirectError, match="禁止创建新版"):
        refresh(pilot, row["outbox_id"])
    assert len(posts) == 1


def _old_v2_query(pilot, row, spelling):
    settings, sessions, *_ = pilot
    with sessions.begin() as db:
        job = db.get(NotificationOutbox, row["outbox_id"])
        destination, _ = bridge._route(db, settings, PROFILE)
        family = _family_key(_source_identity({**QUERY, "timeGranularity": spelling}), destination)
        job.idempotency_key = f"sparkx-page:v2:{family}:0"
        job.payload = {**job.payload, "sparkx_export": {"family": family, "previous_outbox_id": None}}


def test_prior_v2_alias_is_reused_without_rekeying_its_original_uuid(pilot):
    _, sessions, _, provider, posts, _ = pilot
    row = prepare(pilot, enqueue=True)
    _old_v2_query(pilot, row, "DAILY")
    provider["lose_receipt"] = True
    assert worker.process_outbox_batch()["unknown"] == 1
    provider["lose_receipt"] = False
    with sessions() as db:
        key = db.get(NotificationOutbox, row["outbox_id"]).idempotency_key
    assert posts[0]["uuid"] == hashlib.sha256(key.encode()).hexdigest()[:40]
    assert prepare(pilot, enqueue=True) == {**row, "status": "DELIVERY_UNKNOWN"}
    assert worker.process_outbox_batch()["sent"] == 0 and len(posts) == 1
    with sessions() as db:
        assert db.get(NotificationOutbox, row["outbox_id"]).idempotency_key == key


def test_competing_historical_v2_alias_chains_remain_separate_and_block_fresh_export(pilot):
    settings, sessions, _, _, posts, _ = pilot
    a = prepare(pilot, enqueue=True)
    _old_v2_query(pilot, a, "Daily")
    with sessions.begin() as db:
        old = db.get(NotificationOutbox, a["outbox_id"])
        dest, _ = bridge._route(db, settings, PROFILE)
        family, _ = export_keys(QUERY, dest, "101")
        b = NotificationOutbox(idempotency_key=f"sparkx-page:v2:{family}:0", chat_id=old.chat_id,
            status="EXPIRED", payload={**old.payload, "sparkx_export": {"family": family, "previous_outbox_id": None}})
        old.status = "DELIVERY_UNKNOWN"
        db.add(b)
        db.flush()
        b_id = b.id
    info = prepare(pilot, inspect=True)
    assert info["outbox_id"] == b_id and info["blocking_outbox_id"] == a["outbox_id"]
    with pytest.raises(SparkXDirectError, match="多个历史版本"):
        refresh(pilot, b_id)
    assert not posts


def test_hourly_case_aliases_share_history_but_not_daily(pilot):
    a = prepare(pilot, {**QUERY, "timeGranularity": "Hourly"}, enqueue=True)
    assert prepare(pilot, {**QUERY, "timeGranularity": "hOuRlY"}, enqueue=True) == a
    assert prepare(pilot, {**QUERY, "timeGranularity": "daily"}, inspect=True)["status"] == "NO_EXPORT"
