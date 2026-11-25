"""Outbox worker: write-ahead fence, pre-send re-checks and no automatic resend."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from jx_alert.notifications import worker
from jx_alert.notifications.outbox import DELIVERY_UNKNOWN, NotificationOutbox, enqueue_notification
from jx_alert.reports import lanes
from jx_alert.storage.models import NotificationLog
from lane_fakes import HOUR, SEND, runner_for
from test_report_lanes import enable, map_operators


class FakeClient:
    def __init__(self, fail_after_fence=None):
        self.fail_after_fence = fail_after_fence
        self.sent = []

    def _post(self, kind, before_submit, **details):
        before_submit({"operation": kind, "body_sha256": "f" * 64, "provider_uuid": "u" * 40,
                       "app_identity_sha256": "a" * 64})
        if self.fail_after_fence:
            raise self.fail_after_fence
        self.sent.append((kind, details))
        return {"message_id": f"om_{len(self.sent)}"}

    def send_card(self, card, *, receive_id, receive_id_type, open_ids, idempotency_key, before_submit,
                  mention_labels=None):
        return self._post("create", before_submit, receive_id=receive_id, receive_id_type=receive_id_type, card=card,
                          open_ids=list(open_ids), labels=dict(mention_labels or {}))

    def send_text(self, text, *, receive_id, receive_id_type, open_ids, idempotency_key, before_submit):
        return self._post("create", before_submit, receive_id=receive_id, text=text)

    def reply_card(self, message_id, card, *, idempotency_key, reply_in_thread, before_submit):
        return self._post("reply", before_submit, message_id=message_id, thread=reply_in_thread)

    def send_file(self, data, file_name, *, receive_id, receive_id_type, idempotency_key, before_submit):
        if self.upload_error:
            raise self.upload_error  # refused while uploading: before any message
        return self._post("file", before_submit, receive_id=receive_id, name=file_name, data=data)

    upload_error = None


def cards_sent(client):
    return [details for kind, details in client.sent if kind == "create"]


@pytest.fixture
def at_send(monkeypatch):
    monkeypatch.setattr(worker, "_clock", lambda: SEND + timedelta(minutes=1))


def pin_times(session_factory, when):
    """Rows were inserted at the real wall-clock time; pin them to the simulated one."""
    naive = when.astimezone(timezone.utc).replace(tzinfo=None)
    with session_factory() as db:
        db.execute(update(NotificationOutbox).values(created_at=naive, next_attempt_at=naive))
        db.commit()


def queue_daily(settings, session_factory):
    """Default settings: the group copy first, then the company lead."""
    with session_factory() as db:
        result = lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for())
        db.commit()
    pin_times(session_factory, SEND)
    return result["outbox_ids"][0]


def run(settings, session_factory, client, **kwargs):
    return worker.process_outbox_batch(settings=settings, session_factory=session_factory,
                                       client_factory=lambda s: client, **kwargs)


def row(session_factory, row_id):
    with session_factory() as db:
        return db.get(NotificationOutbox, row_id)


def test_queued_daily_report_is_delivered_after_the_intent_is_committed(settings, session_factory, at_send):
    row_id = queue_daily(settings, session_factory)
    client = FakeClient()
    result = run(settings, session_factory, client)
    assert result["sent"] == 4  # two cards, each followed by its Excel copy
    assert [(kind, details["receive_id"]) for kind, details in client.sent] == [
        ("create", "oc_reports"), ("file", "oc_reports"), ("create", "ou_lead"), ("file", "ou_lead")]
    assert [(details["receive_id"], details["open_ids"]) for details in cards_sent(client)] == [
        ("oc_reports", []), ("ou_lead", [])]
    excel = [details for kind, details in client.sent if kind == "file"]
    assert excel[0]["name"] == "广告日报_2026-11-22.xlsx" and excel[0]["data"][:2] == b"PK"
    delivered = row(session_factory, row_id)
    assert delivered.status == "DELIVERED" and delivered.payload["delivery_intent"]["destination"] == "oc_reports"
    assert delivered.payload["delivery_intent"]["body_sha256"] == "f" * 64
    with session_factory() as db:
        assert db.scalar(select(NotificationLog.status).where(NotificationLog.message_id == "om_1")) == "DELIVERED"
    assert run(settings, session_factory, FakeClient())["claimed"] == 0


def test_group_alerts_mention_only_the_operators_still_mapped_when_sending(settings, session_factory, monkeypatch):
    monkeypatch.setattr(worker, "_clock", lambda: HOUR + timedelta(minutes=2))
    map_operators(settings, {"101": ("ou_op1", "皮泽霖"), "102": ("ou_op2", "高爽")})
    with session_factory() as db:
        lanes.run_hourly(db, settings, now=HOUR, runner=runner_for())
        db.commit()
    pin_times(session_factory, HOUR)
    map_operators(settings, {"101": ("ou_new", "新运营")})  # 101 changed hands after queueing
    client = FakeClient()
    run(settings, session_factory, client)
    assert [(details["receive_id"], details["open_ids"], details["labels"]) for details in cards_sent(client)] == [
        ("oc_reports", ["ou_op2"], {"ou_op2": "Campx"}), ("ou_adlead", [], {})]


def test_a_console_test_send_is_delivered_right_away_and_only_once(settings, session_factory, monkeypatch):
    from jx_alert.reports import cards

    later = SEND + timedelta(hours=6)  # 14:30 Beijing: the daily report's own window has closed
    monkeypatch.setattr(worker, "_clock", lambda: later + timedelta(seconds=20))
    enable(settings, daily={"enabled": False})
    values = cards.report_cards("广告日报 · 2026-11-22", "正文")
    with session_factory() as db:
        lanes.queue_test(db, settings, "daily", values, now=later, preview_id="job-9", stores={}, to_group=True,
                         to_lead=False, mention=False)
        db.commit()
    pin_times(session_factory, later)
    client = FakeClient()
    assert run(settings, session_factory, client)["sent"] == 1
    _, details = client.sent[0]
    assert details["receive_id"] == "oc_reports" and details["card"]["header"]["title"]["content"].startswith("【测试】")
    assert run(settings, session_factory, FakeClient())["claimed"] == 0


def test_messages_from_the_retired_release_are_kept_but_never_sent(settings, session_factory, at_send):
    with session_factory() as db:
        old = enqueue_notification(db, idempotency_key="scheduled-daily:morning:2026-11-24:x", text="旧版日报",
                                   delivery={"role": "group", "receive_id_type": "chat_id", "receive_id": "oc_reports",
                                             "open_ids": []})
        db.commit()
    pin_times(session_factory, SEND)
    client = FakeClient()
    run(settings, session_factory, client)
    kept = row(session_factory, old.id)
    assert kept.status == "SKIPPED" and "已停用" in kept.last_error and client.sent == []


def test_a_switch_turned_off_after_queueing_blocks_the_send(settings, session_factory, at_send):
    row_id = queue_daily(settings, session_factory)
    enable(settings, daily={"enabled": False})
    client = FakeClient()
    run(settings, session_factory, client)
    assert row(session_factory, row_id).status == "SKIPPED" and client.sent == []


def test_failure_after_the_fence_is_never_resent(settings, session_factory, at_send):
    row_id = queue_daily(settings, session_factory)
    run(settings, session_factory, FakeClient(fail_after_fence=TimeoutError()))
    held = row(session_factory, row_id)
    assert held.status == DELIVERY_UNKNOWN and "禁止自动重发" in held.last_error
    client = FakeClient()
    assert run(settings, session_factory, client)["claimed"] == 0 and client.sent == []


def test_a_definite_feishu_refusal_is_recorded_with_its_reason_and_never_resent(settings, session_factory, at_send):
    from jx_alert.notifications.feishu_app import FeishuRejected

    row_id = queue_daily(settings, session_factory)
    refusal = FeishuRejected("Feishu app HTTP request rejected: HTTP 400, code=230013", 230013)
    result = run(settings, session_factory, FakeClient(fail_after_fence=refusal))
    refused = row(session_factory, row_id)
    assert refused.status == "REJECTED" and result["failed"] == 4 and result["unknown"] == 0  # cards and Excel
    assert "飞书拒绝，未送达（code=230013" in refused.last_error and "可用范围" in refused.last_error
    client = FakeClient()
    assert run(settings, session_factory, client)["claimed"] == 0 and client.sent == []


def test_an_excel_copy_is_sent_only_while_its_file_is_unchanged(settings, session_factory, at_send):
    from pathlib import Path

    queue_daily(settings, session_factory)
    with session_factory() as db:
        files = [item for item in db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))
                 if item.payload["transport"] == lanes.FILE_TRANSPORT]
    Path(settings.report_archive_dir, files[0].payload["file"]["path"]).write_bytes(b"PK changed")
    client = FakeClient()
    run(settings, session_factory, client)
    # Both copies point at the same saved file: neither goes out once it changed; the cards still do.
    assert len(cards_sent(client)) == 2 and [kind for kind, _ in client.sent].count("file") == 0
    for item in files:
        kept = row(session_factory, item.id)
        assert kept.status == "SKIPPED" and "Excel 文件缺失或内容已变化" in kept.last_error


def test_a_refused_excel_upload_is_recorded_with_its_reason(settings, session_factory, at_send):
    from jx_alert.notifications.feishu_app import FeishuUploadRejected

    queue_daily(settings, session_factory)
    client = FakeClient()
    client.upload_error = FeishuUploadRejected("Feishu file upload rejected: code=99991672", 99991672)
    run(settings, session_factory, client)
    with session_factory() as db:
        found = list(db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    assert [item.status for item in found] == ["DELIVERED", "REJECTED", "DELIVERED", "REJECTED"]
    assert "飞书拒绝接收 Excel 文件" in found[1].last_error and "im:resource" in found[1].last_error
    assert run(settings, session_factory, FakeClient())["claimed"] == 0  # a refusal is not retried


def test_a_file_row_without_its_file_record_is_never_sent(settings, session_factory, at_send):
    with session_factory() as db:
        forged = enqueue_notification(db, idempotency_key="jx:daily:2026-11-24:forged:file", text="Excel", delivery={
            "role": "jx_daily_group", "receive_id_type": "chat_id", "receive_id": "oc_reports", "open_ids": []},
            extra={"jx_lane": "daily", "jx_day": "2026-11-24", "transport": lanes.FILE_TRANSPORT})
        db.commit()
    pin_times(session_factory, SEND)
    client = FakeClient()
    run(settings, session_factory, client)
    assert row(session_factory, forged.id).status == "SKIPPED" and client.sent == []


def test_failure_before_any_post_is_retried_with_backoff(settings, session_factory, at_send):
    row_id = queue_daily(settings, session_factory)

    def broken(settings_):
        raise RuntimeError("credentials unavailable")

    worker.process_outbox_batch(settings=settings, session_factory=session_factory, client_factory=broken)
    waiting = row(session_factory, row_id)
    assert waiting.status == "PRE_SEND_RETRY" and waiting.attempts == 1
    assert waiting.next_attempt_at > (SEND + timedelta(minutes=1)).replace(tzinfo=None)


def test_expired_window_and_stale_submissions(settings, session_factory, monkeypatch):
    row_id = queue_daily(settings, session_factory)
    monkeypatch.setattr(worker, "_clock", lambda: SEND + timedelta(hours=4))
    run(settings, session_factory, FakeClient())
    assert row(session_factory, row_id).status == "EXPIRED"
    with session_factory() as db:
        stale = enqueue_notification(db, idempotency_key="jx:daily:2026-11-24:stale", text="x", delivery={
            "role": "jx_daily_lead", "receive_id_type": "open_id", "receive_id": "ou_lead", "open_ids": []},
            extra={"jx_lane": "daily", "jx_day": "2026-11-24"})
        stale.status, stale.claimed_at = "SUBMITTING", (SEND - timedelta(hours=1)).replace(tzinfo=None)
        db.commit()
    run(settings, session_factory, FakeClient())
    assert row(session_factory, stale.id).status == DELIVERY_UNKNOWN


def test_dry_run_sends_nothing(settings, session_factory, at_send):
    queue_daily(settings, session_factory)
    dry = settings.model_copy(update={"app_dry_run": True})
    assert worker.process_outbox_batch(settings=dry, session_factory=session_factory,
                                       client_factory=lambda s: pytest.fail("no client"))["skipped"] is True


def test_question_replies_use_the_original_message(settings, session_factory, monkeypatch):
    monkeypatch.setattr(worker, "_clock", lambda: datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc))
    import jx_alert.bot_service as bot_service
    monkeypatch.setattr(bot_service, "validate_question_delivery", lambda s, d: d.get("receive_id") == "oc_reports")
    with session_factory() as db:
        reply = enqueue_notification(db, idempotency_key="group-question:om_q:0", text="回答", card={
            "schema": "2.0", "header": {"title": {"content": "回答"}}, "body": {"elements": []}}, delivery={
            "role": "group_question", "receive_id_type": "chat_id", "receive_id": "oc_reports", "open_ids": [],
            "reply_to_message_id": "om_q"})
        db.commit()
    pin_times(session_factory, datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc))
    client = FakeClient()
    run(settings, session_factory, client, question_replies_only=True)
    assert client.sent == [("reply", {"message_id": "om_q", "thread": True})]
    assert row(session_factory, reply.id).status == "DELIVERED"
