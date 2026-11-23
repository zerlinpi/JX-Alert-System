from datetime import datetime, timedelta, timezone

import pytest

from jx_alert import ad_source_policy as policy
from test_hourly_ad_monitor import warehouse  # noqa: F401 (shared fixture)


NOW = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)


def test_restoring_lingxing_does_not_revive_old_pending_or_held_rows(monkeypatch):
    controls = {
        "1": {"source": "lingxing", "selected_at": NOW.isoformat()},
        "2": {"source": "lingxing", "selected_at": None},
        "3": {"source": "sparkx", "selected_at": NOW.isoformat()},
    }
    monkeypatch.setattr(policy, "source_snapshot", lambda _, sid: controls[sid])
    old, fresh = NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)
    assert policy.lingxing_ad_allowed(None, "1")
    assert not policy.lingxing_ad_allowed(None, "3")
    assert not policy.lingxing_ad_allowed(None, "999")
    assert policy.legacy_payload_allowed(None, {"store_ids": ["1", "2"]}, fresh)
    assert not policy.legacy_payload_allowed(None, {"store_ids": ["1", "2"]}, old)
    assert not policy.legacy_payload_allowed(None, {"store_ids": ["1", "2"]}, NOW)
    assert not policy.legacy_payload_allowed(None, {"store_ids": ["3"]}, fresh)
    held = {"store_id": "1", "detected_at": old.isoformat()}
    assert not policy.legacy_payload_allowed(None, {"hourly_items": [held]}, fresh)
    updated = {**held, "detected_at": fresh.isoformat()}
    assert policy.legacy_payload_allowed(None, {"hourly_items": [updated]}, fresh)


@pytest.mark.parametrize("payload", [
    {"morning_message_kind": "health"},
    {"morning_message_kind": "business_daily"},
    {"morning_message_kind": "owner_daily"},
    {"delivery": {"role": "sparkx_performance_page"}},
    {"delivery": {"role": "private_question", "reply_to_message_id": "om_fixture"}},
])
def test_unrelated_reports_and_manual_exports_keep_existing_behavior(payload):
    assert not policy.legacy_ad_payload(payload)


@pytest.mark.parametrize("payload", [
    {"morning_message_kind": "ad_exception"}, {"hourly_ad_monitor": True}, {"material_ad_alert": True},
])
def test_only_legacy_proactive_advertising_is_fenced(payload):
    assert policy.legacy_ad_payload(payload)


@pytest.mark.parametrize("switch_at", ["token", "commit"])
def test_switch_before_feishu_post_stops_old_ad_payload(request, monkeypatch, switch_at):
    from sqlalchemy import select

    from jx_alert.notifications import hourly_ads, worker
    from jx_alert.notifications.feishu_app import FeishuAppClient
    from jx_alert.notifications.outbox import NotificationOutbox
    from test_hourly_ad_monitor import CLOCK, _finding

    settings, sessions, _, _ = request.getfixturevalue("warehouse")
    selection = {"source": "lingxing", "selected_at": None}
    posted = []

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK.astimezone(tz) if tz else CLOCK.replace(tzinfo=None)

    class Feishu:
        def send_card(self, card, *, before_submit, **kwargs):
            if switch_at == "token":
                selection.update(source="sparkx", selected_at=CLOCK.isoformat())
            before_submit({})
            posted.append(card)
            return {"message_id": "om_should_never_post"}

    monkeypatch.setattr(policy, "source_snapshot", lambda *_: selection)
    monkeypatch.setattr(worker, "datetime", FixedClock)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "load_runtime_settings", lambda: settings)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda _: Feishu())
    record = worker._record_submission

    def switch_after_commit(*args):
        result = record(*args)
        if switch_at == "commit":
            selection.update(source="sparkx", selected_at=CLOCK.isoformat())
        return result

    monkeypatch.setattr(worker, "_record_submission", switch_after_commit)
    with sessions() as db:
        row_id, = hourly_ads.queue_hourly_findings(
            db, settings, [_finding("5174", "active")], now=CLOCK, slot="fixture", send_alerts=True,
        )
        row = db.get(NotificationOutbox, row_id)
        row.next_attempt_at = row.created_at = CLOCK.replace(tzinfo=None)
        db.commit()
    assert worker.process_outbox_batch(outbox_ids=[row_id])["sent"] == 0
    assert posted == []
    with sessions() as db:
        row = db.scalar(select(NotificationOutbox))
        assert row.status == "SKIPPED" and row.last_error == policy.BLOCKED
        if switch_at == "token":
            assert "delivery_intent" not in row.payload
        else:
            assert row.payload["delivery_intent"]["aborted_before_post"] is True
    assert worker.process_outbox_batch(outbox_ids=[row_id])["claimed"] == 0
