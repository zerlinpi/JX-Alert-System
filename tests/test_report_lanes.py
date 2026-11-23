"""Queueing the SparkX lanes and re-checking each frozen recipient before sending."""
import copy
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from conftest import AD_LEAD, LEAD, write_private
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.reports import lanes
from jx_alert.reports import settings as lane_settings
from jx_alert.sparkx import stores
from jx_alert.sparkx.client import SparkXError
from jx_alert.storage.models import SyncJob
from test_sparkx_analysis import DAILY, HOURLY, REVIEW

SEND = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)  # 08:30 Beijing
HOUR = datetime(2026, 11, 24, 6, 5, tzinfo=timezone.utc)   # 14:05 Beijing


def enable(settings, **changes):
    current = lane_settings.load(settings)
    lanes_ = copy.deepcopy(current["lanes"])
    for lane, values in changes.items():
        lanes_[lane].update(values)
    lane_settings.save(settings, {"revision": current["revision"], "lanes": lanes_})


def map_operators(settings, mapping):
    directory = stores.load(settings)
    stores.save(settings, {pid: {"monitored": True, "operators": [{"open_id": uid, "name": name}]}
                           for pid, (uid, name) in mapping.items()}, directory["revision"])


def collector_for(data, calls=None):
    def collect(settings, lane, *, now, directory, on_roster):
        if calls is not None:
            calls.append(lane)
        on_roster(data["roster"])
        return copy.deepcopy(data)
    return collect


def rules(settings, report):
    return ([{"title": "", "text": text} for text in report["notes"]], "rules") if "notes" in report else (
        {"headline": None, "actions": report["actions"]}, "rules")


def rows(db):
    return list(db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))


def test_daily_report_goes_privately_to_the_company_lead_once(settings, session_factory):
    enable(settings, daily={"enabled": True})
    calls = []
    with session_factory() as db:
        result = lanes.queue_morning_lane(db, settings, "daily", now=SEND, collector=collector_for(DAILY, calls),
                                          narrator=rules)
        db.commit()
        assert result["status"] == "QUEUED" and len(result["outbox_ids"]) == 1
        row = rows(db)[0]
        assert row.payload["delivery"]["role"] == "jx_daily_lead" and row.chat_id == LEAD
        assert row.payload["jx_lane"] == "daily" and row.payload["jx_day"] == "2026-11-24"
        assert row.payload["card"]["header"]["title"]["content"] == "SparkX 广告日报 · 2026-11-22"
        again = lanes.queue_morning_lane(db, settings, "daily", now=SEND + timedelta(minutes=5),
                                         collector=collector_for(DAILY, calls), narrator=rules)
        assert again["status"] == "ALREADY_COMPLETE" and calls == ["daily"]
        roster = db.scalar(select(SyncJob).where(SyncJob.job_type == "sparkx_roster"))
        assert roster is not None and roster.metadata_json["profiles"]["101"]["name"] == "香港川页"


def test_group_copy_and_switches(settings, session_factory):
    with session_factory() as db:
        assert lanes.queue_morning_lane(db, settings, "daily", now=SEND, collector=collector_for(DAILY))["status"] == "DISABLED"
    enable(settings, daily={"enabled": True, "to_group": True})
    with session_factory() as db:
        early = lanes.queue_morning_lane(db, settings, "daily", now=SEND - timedelta(minutes=1),
                                         collector=collector_for(DAILY))
        assert early["status"] == "WAITING"
        result = lanes.queue_morning_lane(db, settings, "daily", now=SEND, collector=collector_for(DAILY),
                                          narrator=rules)
        db.commit()
        assert [row.payload["delivery"]["role"] for row in rows(db)] == ["jx_daily_group", "jx_daily_lead"]
        assert len(result["outbox_ids"]) == 2
    dry = settings.model_copy(update={"app_dry_run": True})
    with session_factory() as db:
        assert lanes.queue_morning_lane(db, dry, "daily", now=SEND, collector=collector_for(DAILY))["status"] == "DELIVERY_DISABLED"


def test_review_goes_to_the_ad_lead_and_each_operator_gets_only_their_stores(settings, session_factory):
    enable(settings, review={"enabled": True})
    map_operators(settings, {"101": ("ou_op1", "皮泽霖"), "102": ("ou_op2", "高爽"), "104": ("ou_op1", "皮泽霖")})
    with session_factory() as db:
        result = lanes.queue_morning_lane(db, settings, "review", now=SEND, collector=collector_for(REVIEW),
                                          narrator=rules)
        db.commit()
        queued = rows(db)
        assert result["status"] == "QUEUED"
        assert [(row.payload["delivery"]["role"], row.chat_id) for row in queued] == [
            ("jx_review_lead", AD_LEAD), ("jx_review_operator", "ou_op1"), ("jx_review_operator", "ou_op2")]
        lead_card = json.dumps(queued[0].payload["card"], ensure_ascii=False)
        assert "皮泽霖" in lead_card and "高爽" in lead_card and "昌泉" in lead_card
        op1 = json.dumps(queued[1].payload["card"], ensure_ascii=False)
        assert queued[1].payload["delivery"]["profiles"] == ["101"]  # 104 has no finding
        assert "香港川页" in op1 and "Campx" not in op1 and "昌泉" not in op1
        receipt = db.scalar(select(SyncJob).where(SyncJob.job_type == "jx_review"))
        assert receipt.status == "SUCCEEDED" and receipt.metadata_json["findings"] == 5


def test_a_lead_who_also_operates_a_store_gets_one_full_message(settings, session_factory):
    enable(settings, review={"enabled": True})
    map_operators(settings, {"101": (AD_LEAD, "广告主管")})
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, collector=collector_for(REVIEW), narrator=rules)
        db.commit()
        assert [row.chat_id for row in rows(db)] == [AD_LEAD]


def test_sparkx_failure_is_recorded_and_retried_with_backoff(settings, session_factory):
    enable(settings, daily={"enabled": True})

    def failing(*args, **kwargs):
        raise SparkXError("SPARKX_RATE_LIMITED")

    with session_factory() as db:
        first = lanes.queue_morning_lane(db, settings, "daily", now=SEND, collector=failing)
        db.commit()
        assert first == {"status": "FAILED", "error": "SPARKX_RATE_LIMITED"}
        assert lanes.queue_morning_lane(db, settings, "daily", now=SEND + timedelta(minutes=5),
                                        collector=failing)["status"] == "RETRY_LATER"
        later = lanes.queue_morning_lane(db, settings, "daily", now=SEND + timedelta(minutes=11),
                                         collector=collector_for(DAILY), narrator=rules)
        assert later["status"] == "QUEUED"


def test_hourly_monitor_alerts_each_campaign_category_once_per_day(settings, session_factory):
    enable(settings, hourly={"enabled": True})
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    with session_factory() as db:
        first = lanes.run_hourly(db, settings, now=HOUR, collector=collector_for(HOURLY))
        db.commit()
        assert first["status"] == "QUEUED"
        assert [(row.payload["delivery"]["role"], row.chat_id) for row in rows(db)] == [
            ("jx_hourly_lead", AD_LEAD), ("jx_hourly_operator", "ou_op2")]
        assert lanes.run_hourly(db, settings, now=HOUR + timedelta(minutes=20),
                                collector=collector_for(HOURLY))["status"] == "ALREADY_COMPLETE"
        second = lanes.run_hourly(db, settings, now=HOUR + timedelta(hours=1), collector=collector_for(HOURLY))
        db.commit()
        assert second["status"] == "NO_FINDINGS" and len(rows(db)) == 2
        grown = copy.deepcopy(HOURLY)
        grown["campaigns"]["101"]["3"]["t"].update(spend=35.0)  # low → high spend without orders
        third = lanes.run_hourly(db, settings, now=HOUR + timedelta(hours=2), collector=collector_for(grown))
        db.commit()
        assert third["status"] == "QUEUED" and len(rows(db)) == 3
        assert "C" in json.dumps(rows(db)[-1].payload["card"], ensure_ascii=False)
    enable(settings, hourly={"start_hour": 9, "end_hour": 12})
    with session_factory() as db:
        assert lanes.run_hourly(db, settings, now=HOUR + timedelta(hours=3),
                                collector=collector_for(HOURLY))["status"] == "OUTSIDE_HOURS"


def test_delivery_windows_are_derived_from_the_frozen_payload():
    payload = {"jx_lane": "daily", "jx_day": "2026-11-24"}
    key = "jx:daily:2026-11-24:abc"
    assert lanes.delivery_window(key, payload, SEND - timedelta(minutes=1))[0] == "WAITING"
    assert lanes.delivery_window(key, payload, SEND)[0] == "READY"
    assert lanes.delivery_window(key, payload, SEND + timedelta(hours=3, minutes=30))[0] == "EXPIRED"
    assert lanes.delivery_window("jx:daily:2026-11-23:abc", payload, SEND)[0] == "EXPIRED"
    hourly = {"jx_lane": "hourly", "jx_slot": "2026-11-24T14", "jx_created_at": HOUR.isoformat()}
    assert lanes.delivery_window("jx:hourly:2026-11-24T14:x", hourly, HOUR + timedelta(minutes=49))[0] == "READY"
    assert lanes.delivery_window("jx:hourly:2026-11-24T14:x", hourly, HOUR + timedelta(minutes=51))[0] == "EXPIRED"


def test_recipient_changes_block_older_frozen_messages(settings, session_factory, routing):
    enable(settings, review={"enabled": True})
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, collector=collector_for(REVIEW), narrator=rules)
        db.commit()
        lead_row, operator_row = rows(db)
    assert lanes.delivery_blocked(settings, lead_row.idempotency_key, lead_row.payload, SEND) is None
    assert lanes.delivery_blocked(settings, operator_row.idempotency_key, operator_row.payload, SEND) is None
    changed = copy.deepcopy(routing)
    changed["ad_alert_subscribers"]["recipients"][0]["generation"] = "c" * 32
    write_private(settings_path(settings), changed)
    assert "负责人已变更" in lanes.delivery_blocked(settings, lead_row.idempotency_key, lead_row.payload, SEND)
    map_operators(settings, {"102": ("ou_op1", "皮泽霖")})
    assert "运营已变更" in lanes.delivery_blocked(settings, operator_row.idempotency_key, operator_row.payload, SEND)
    enable(settings, review={"to_operators": False})
    assert "发送对象已关闭" in lanes.delivery_blocked(settings, operator_row.idempotency_key, operator_row.payload, SEND)
    enable(settings, review={"enabled": False})
    assert "已停用" in lanes.delivery_blocked(settings, lead_row.idempotency_key, lead_row.payload, SEND)


def settings_path(settings):
    from pathlib import Path
    return Path(settings.feishu_routing_file)


def test_settings_validation_rejects_inverted_thresholds(settings):
    current = lane_settings.load(settings)
    bad = copy.deepcopy(current["lanes"])
    bad["hourly"].update(low_spend=50, high_spend=30)
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.save(settings, {"revision": current["revision"], "lanes": bad})
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.save(settings, {"revision": "1" * 64, "lanes": current["lanes"]})
