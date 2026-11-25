"""Queueing the lanes (DeepSeek's answers stubbed) and re-checking each frozen recipient before sending."""
import copy
from datetime import timedelta
import io
import json
import re
import zipfile

import pytest
from sqlalchemy import select

from conftest import AD_LEAD, GROUP_CHAT, LEAD, write_private
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.reports import cards, lanes
from jx_alert.reports import settings as lane_settings
from jx_alert.sparkx import stores
from jx_alert.sparkx.client import SparkXError
from jx_alert.storage.models import SyncJob
from lane_fakes import HOUR, HOURLY_FINDINGS, SEND, finding, lane_result, runner_for


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


def rows(db, *, excel=False):
    """Queued card messages (or, with ``excel``, their Excel copies), in queue order."""
    found = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))
    return [row for row in found if (row.payload.get("transport") == lanes.FILE_TRANSPORT) == excel]


def routes(db):
    return [(row.payload["delivery"]["role"], row.chat_id, row.payload["delivery"].get("open_ids", []))
            for row in rows(db)]


def files(db):
    return [(row.payload["delivery"]["role"], row.chat_id, row.payload["file"]["name"]) for row in rows(db, excel=True)]


def test_every_lane_is_on_by_default_and_reaches_the_group():
    defaults = lane_settings.DEFAULTS
    assert all(defaults[lane]["enabled"] and defaults[lane]["to_group"] and defaults[lane]["to_lead"]
               and defaults[lane]["mention_operators"] for lane in lane_settings.LANES)
    assert not defaults["review"]["to_operators"] and not defaults["hourly"]["to_operators"]
    assert all(defaults[lane]["prompt"] == "" for lane in lane_settings.LANES)  # empty = built-in default text


def test_daily_report_goes_to_the_group_and_the_company_lead_once(settings, session_factory):
    calls = []
    with session_factory() as db:
        result = lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for(calls=calls))
        db.commit()
        assert result["status"] == "QUEUED" and len(result["outbox_ids"]) == 4  # a card and its Excel copy each
        assert routes(db) == [("jx_daily_group", GROUP_CHAT, []), ("jx_daily_lead", LEAD, [])]
        assert files(db) == [("jx_daily_group", GROUP_CHAT, "广告日报_2026-11-22.xlsx"),
                             ("jx_daily_lead", LEAD, "广告日报_2026-11-22.xlsx")]
        row = rows(db)[1]
        assert row.payload["jx_lane"] == "daily" and row.payload["jx_day"] == "2026-11-24"
        card = row.payload["card"]
        assert card["header"]["title"]["content"] == "广告日报 · 2026-11-22"
        tags = [element["tag"] for element in card["body"]["elements"]]
        assert tags.count("table") == 2  # the agent's Markdown tables became Feishu tables
        detail = [element for element in card["body"]["elements"] if element["tag"] == "table"][1]
        headers = [column["display_name"] for column in detail["columns"]]
        assert headers[-1] == "运营" and headers[headers.index("广告销量"):headers.index("广告销量") + 2] == [
            "广告销量", "总销量"]
        assert detail["rows"][0][f"c{headers.index('总订单')}"] == "—"  # not yet updated: missing, not 0
        dumped = json.dumps(card, ensure_ascii=False)
        assert not any(word in dumped for word in ("SparkX", "DeepSeek", "只读"))  # no source or method footer
        again = lanes.queue_morning_lane(db, settings, "daily", now=SEND + timedelta(minutes=5),
                                         runner=runner_for(calls=calls))
        assert again["status"] == "ALREADY_COMPLETE" and calls == ["daily"]
        roster = db.scalar(select(SyncJob).where(SyncJob.job_type == "sparkx_roster"))
        assert roster is not None and roster.metadata_json["profiles"]["101"]["name"] == "香港川页"
        receipt = db.scalar(select(SyncJob).where(SyncJob.job_type == "jx_daily"))
        assert receipt.metadata_json["sparkx_calls"] == 4 and receipt.metadata_json["model"] == "deepseek-v4-pro"


def test_switches_window_dry_run_and_missing_credentials(settings, session_factory):
    enable(settings, daily={"enabled": False})
    with session_factory() as db:
        assert lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for())["status"] == "DISABLED"
    enable(settings, daily={"enabled": True, "to_group": False})
    with session_factory() as db:
        early = lanes.queue_morning_lane(db, settings, "daily", now=SEND - timedelta(minutes=1), runner=runner_for())
        assert early["status"] == "WAITING"
        lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for())
        db.commit()
        assert routes(db) == [("jx_daily_lead", LEAD, [])]
    dry = settings.model_copy(update={"app_dry_run": True})
    blank = settings.model_copy(update={"sparkx_ai_token": ""})
    keyless = settings.model_copy(update={"deepseek_api_key": ""})
    with session_factory() as db:
        assert lanes.queue_morning_lane(db, dry, "review", now=SEND, runner=runner_for())["status"] == "DELIVERY_DISABLED"
        assert lanes.queue_morning_lane(db, blank, "review", now=SEND)["status"] == "SPARKX_NOT_CONFIGURED"
        assert lanes.run_hourly(db, blank, now=HOUR)["status"] == "SPARKX_NOT_CONFIGURED"
        assert lanes.queue_morning_lane(db, keyless, "review", now=SEND)["status"] == "DEEPSEEK_NOT_CONFIGURED"
        assert db.scalar(select(SyncJob.id).where(SyncJob.job_type == "jx_review")) is None  # nothing recorded


def test_review_still_notifies_when_no_operator_is_mapped(settings, session_factory):
    with session_factory() as db:
        result = lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for())
        db.commit()
        assert result["status"] == "QUEUED"
        assert routes(db) == [("jx_review_group", GROUP_CHAT, []), ("jx_review_lead", AD_LEAD, [])]
        text = json.dumps(rows(db)[0].payload["card"], ensure_ascii=False)
        assert "未映射运营" in text and "JX_STORES" not in text
        assert rows(db)[0].payload["card"]["header"]["template"] == "orange"


def test_review_group_message_mentions_the_operators_of_affected_stores(settings, session_factory):
    map_operators(settings, {"101": ("ou_op1", "皮泽霖"), "102": ("ou_op2", "高爽"), "104": ("ou_op1", "皮泽霖")})
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for())
        db.commit()
        group, lead = rows(db)
        assert group.payload["delivery"]["open_ids"] == ["ou_op1", "ou_op2"]
        # 104 is not named by the review, so it is not a reason to mention anyone.
        assert group.payload["delivery"]["mentions"] == {"ou_op1": ["101"], "ou_op2": ["102"]}
        assert group.payload["delivery"]["mention_names"] == {"101": "香港川页", "102": "Campx"}
        assert lead.payload["delivery"]["open_ids"] == [] and len(rows(db)) == 2  # private operator copies are off
        # The Excel copies never @ anyone.
        assert all(row.payload["delivery"]["open_ids"] == [] for row in rows(db, excel=True))
        overview = next(element for element in group.payload["card"]["body"]["elements"] if element["tag"] == "table")
        # The model wrote 运营 as the second column; the card shows it last.
        assert [column["display_name"] for column in overview["columns"]][-1] == "运营"
        assert overview["rows"][0]["c0"] == "香港川页" and overview["rows"][0]["c6"] == "未映射运营"
    assert lanes.mention_plan(settings, group.payload) == (["ou_op1", "ou_op2"], {"ou_op1": "香港川页", "ou_op2": "Campx"})


def test_daily_report_mentions_the_operators_of_the_stores_it_flags(settings, session_factory):
    map_operators(settings, {"102": ("ou_op2", "高爽"), "104": ("ou_op1", "皮泽霖")})
    results = {"daily": lane_result("daily", stores=["102"])}
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for(results))
        db.commit()
        assert routes(db) == [("jx_daily_group", GROUP_CHAT, ["ou_op2"]), ("jx_daily_lead", LEAD, [])]
        group = rows(db)[0]
    assert lanes.mention_plan(settings, group.payload) == (["ou_op2"], {"ou_op2": "Campx"})
    enable(settings, daily={"mention_operators": False})
    assert lanes.mention_plan(settings, group.payload) == ([], {})


def test_operators_outside_the_group_are_not_mentioned(settings, session_factory):
    map_operators(settings, {"101": ("ou_outsider", "外部人员")})
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for())
        db.commit()
        assert rows(db)[0].payload["delivery"]["open_ids"] == []


def test_optional_private_operator_copies_hold_only_their_stores(settings, session_factory):
    enable(settings, review={"to_operators": True})
    map_operators(settings, {"101": ("ou_op1", "皮泽霖"), "102": ("ou_op2", "高爽")})
    asked = []

    def scoper(settings_, text, names):
        asked.append(names)
        return f"只含 {names[0]} 的复盘。" if names[0] == "香港川页" else None

    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for(), scoper=scoper)
        db.commit()
        queued = rows(db)
        assert [(row.payload["delivery"]["role"], row.chat_id) for row in queued] == [
            ("jx_review_group", GROUP_CHAT), ("jx_review_lead", AD_LEAD), ("jx_review_operator", "ou_op1")]
        assert asked == [["香港川页"], ["Campx"]]  # 高爽's copy had nothing left, so none was queued
        assert "只含 香港川页 的复盘" in json.dumps(queued[2].payload["card"], ensure_ascii=False)
        assert queued[2].payload["delivery"]["profiles"] == ["101"]
        # The operator's Excel holds their own copy (its own file), the others share the full one.
        assert files(db) == [("jx_review_group", GROUP_CHAT, "广告异常复盘_2026-11-16至2026-11-22.xlsx"),
                             ("jx_review_lead", AD_LEAD, "广告异常复盘_2026-11-16至2026-11-22.xlsx"),
                             ("jx_review_operator", "ou_op1", "广告异常复盘_2026-11-16至2026-11-22_皮泽霖.xlsx")]


def test_a_lead_who_also_operates_a_store_gets_one_private_message(settings, session_factory):
    enable(settings, review={"to_operators": True})
    map_operators(settings, {"101": (AD_LEAD, "广告主管")})
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for(), scoper=lambda *a: "x")
        db.commit()
        assert routes(db) == [("jx_review_group", GROUP_CHAT, [AD_LEAD]), ("jx_review_lead", AD_LEAD, [])]


@pytest.mark.parametrize("error,code", [(SparkXError("SPARKX_RATE_LIMITED"), "SPARKX_RATE_LIMITED"),
                                        (DeepSeekError("DeepSeek繁忙，请稍后重试。"), "DEEPSEEK_FAILED"),
                                        (ValueError("empty report"), "AGENT_OUTPUT_INVALID")])
def test_failures_are_recorded_and_retried_with_backoff(settings, session_factory, error, code):
    with session_factory() as db:
        first = lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for(error=error))
        db.commit()
        assert first["status"] == "FAILED" and first["error"] == code and first["message"]
        assert lanes.queue_morning_lane(db, settings, "daily", now=SEND + timedelta(minutes=5),
                                        runner=runner_for())["status"] == "RETRY_LATER"
        later = lanes.queue_morning_lane(db, settings, "daily", now=SEND + timedelta(minutes=11), runner=runner_for())
        assert later["status"] == "QUEUED"


def test_a_long_report_is_split_and_only_the_first_card_mentions(settings, session_factory):
    map_operators(settings, {"101": ("ou_op1", "皮泽霖")})
    table = "| 店铺 | 花费 |\n|---|---|\n| 香港川页 | $1 |\n"
    long_text = "\n\n".join(f"**第 {index} 节**\n{table}" for index in range(7)) + "\n"
    results = {"review": lane_result("review", content=long_text, stores=["101"])}
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for(results))
        db.commit()
        group = [row for row in rows(db) if row.payload["delivery"]["role"] == "jx_review_group"]
        assert len(group) == 2 and group[0].payload["card"]["header"]["title"]["content"].endswith("（1/2）")
        assert group[0].payload["delivery"]["open_ids"] == ["ou_op1"] and group[1].payload["delivery"]["open_ids"] == []
        assert group[1].idempotency_key.endswith(":1")


def test_hourly_monitor_alerts_each_campaign_category_once_per_day(settings, session_factory):
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    start = HOUR - timedelta(hours=2)  # 12:05 Beijing; every run below is on 2026-11-23 in Los Angeles
    with session_factory() as db:
        first = lanes.run_hourly(db, settings, now=start, runner=runner_for())
        db.commit()
        assert first["status"] == "QUEUED"
        assert routes(db) == [("jx_hourly_group", GROUP_CHAT, ["ou_op2"]), ("jx_hourly_lead", AD_LEAD, [])]
        card = json.dumps(rows(db)[0].payload["card"], ensure_ascii=False)
        assert "高花费无转化（1）" in card and "低花费高转化（1）" in card and "高爽" in card and "未映射运营" in card
        assert lanes.run_hourly(db, settings, now=start + timedelta(minutes=20),
                                runner=runner_for())["status"] == "ALREADY_COMPLETE"
        second = lanes.run_hourly(db, settings, now=start + timedelta(hours=1), runner=runner_for())
        db.commit()
        assert second["status"] == "NO_FINDINGS" and len(rows(db)) == 2
        grown = HOURLY_FINDINGS + [finding("101", "9002", "high_spend_no_conversion", 35.0, 25, 0)]  # escalated
        third = lanes.run_hourly(db, settings, now=start + timedelta(hours=2),
                                 runner=runner_for({"hourly": lane_result("hourly", findings=grown)}))
        db.commit()
        assert third["status"] == "QUEUED" and len(rows(db)) == 4
        # Only store 101 changed, and it has no operator yet: notified, nobody mentioned.
        assert routes(db)[2] == ("jx_hourly_group", GROUP_CHAT, [])
        # A new day in Los Angeles starts a new round of alerts.
        fresh = lanes.run_hourly(db, settings, now=start + timedelta(hours=4), runner=runner_for())
        assert fresh["status"] == "QUEUED"
    enable(settings, hourly={"start_hour": 9, "end_hour": 12})
    with session_factory() as db:
        assert lanes.run_hourly(db, settings, now=HOUR + timedelta(hours=3),
                                runner=runner_for())["status"] == "OUTSIDE_HOURS"


def test_hourly_receives_what_was_already_alerted(settings, session_factory):
    seen = []

    def runner(settings_, lane, config, *, now, directory, alerted, on_roster):
        seen.append(set(alerted))
        return runner_for()(settings_, lane, config, now=now, directory=directory, alerted=alerted, on_roster=on_roster)

    with session_factory() as db:
        lanes.run_hourly(db, settings, now=HOUR, runner=runner)
        db.commit()
        lanes.run_hourly(db, settings, now=HOUR + timedelta(hours=1), runner=runner)
    assert seen[0] == set() and seen[1] == {"102:9001:high_spend_no_conversion", "101:9002:low_spend_no_conversion",
                                            "101:9003:low_spend_high_conversion"}


def test_mentions_are_rechecked_before_sending(settings, session_factory):
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    with session_factory() as db:
        lanes.run_hourly(db, settings, now=HOUR, runner=runner_for())
        db.commit()
        group, lead = rows(db)
    assert lanes.send_mentions(settings, group.payload) == ["ou_op2"]
    assert lanes.mention_plan(settings, group.payload) == (["ou_op2"], {"ou_op2": "Campx"})
    assert lanes.send_mentions(settings, lead.payload) == []
    map_operators(settings, {"102": ("ou_op1", "皮泽霖")})  # reassigned after queueing
    assert lanes.send_mentions(settings, group.payload) == []
    assert lanes.delivery_blocked(settings, group.idempotency_key, group.payload, HOUR) is None  # still sent
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    enable(settings, hourly={"mention_operators": False})
    assert lanes.send_mentions(settings, group.payload) == []


def test_delivery_windows_are_derived_from_the_frozen_payload():
    payload = {"jx_lane": "daily", "jx_day": "2026-11-24"}
    key = "jx:daily:2026-11-24:abc"
    assert lanes.delivery_window(key, payload, SEND - timedelta(minutes=1))[0] == "WAITING"
    assert lanes.delivery_window(key, payload, SEND)[0] == "READY"
    assert lanes.delivery_window(key + ":1", payload, SEND)[0] == "READY"
    assert lanes.delivery_window(key, payload, SEND + timedelta(hours=3, minutes=30))[0] == "EXPIRED"
    assert lanes.delivery_window("jx:daily:2026-11-23:abc", payload, SEND)[0] == "EXPIRED"
    hourly = {"jx_lane": "hourly", "jx_slot": "2026-11-24T14", "jx_created_at": HOUR.isoformat()}
    assert lanes.delivery_window("jx:hourly:2026-11-24T14:x", hourly, HOUR + timedelta(minutes=49))[0] == "READY"
    assert lanes.delivery_window("jx:hourly:2026-11-24T14:x", hourly, HOUR + timedelta(minutes=51))[0] == "EXPIRED"


def test_recipient_changes_block_older_frozen_messages(settings, session_factory, routing):
    enable(settings, review={"to_operators": True})
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "review", now=SEND, runner=runner_for(), scoper=lambda *a: "Campx 复盘")
        db.commit()
        group_row, lead_row, operator_row = rows(db)
    for row in (group_row, lead_row, operator_row):
        assert lanes.delivery_blocked(settings, row.idempotency_key, row.payload, SEND) is None
    changed = copy.deepcopy(routing)
    changed["ad_alert_subscribers"]["recipients"][0]["generation"] = "c" * 32
    write_private(settings_path(settings), changed)
    assert "负责人已变更" in lanes.delivery_blocked(settings, lead_row.idempotency_key, lead_row.payload, SEND)
    map_operators(settings, {"102": ("ou_op1", "皮泽霖")})
    assert "运营已变更" in lanes.delivery_blocked(settings, operator_row.idempotency_key, operator_row.payload, SEND)
    enable(settings, review={"to_operators": False})
    assert "发送对象已关闭" in lanes.delivery_blocked(settings, operator_row.idempotency_key, operator_row.payload, SEND)
    enable(settings, review={"enabled": False})
    assert "已停用" in lanes.delivery_blocked(settings, group_row.idempotency_key, group_row.payload, SEND)


def settings_path(settings):
    from pathlib import Path
    return Path(settings.feishu_routing_file)


def test_settings_validation(settings):
    current = lane_settings.load(settings)
    bad = copy.deepcopy(current["lanes"])
    bad["hourly"].update(low_spend=50, high_spend=30)
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.save(settings, {"revision": current["revision"], "lanes": bad})
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.save(settings, {"revision": "1" * 64, "lanes": current["lanes"]})
    long_prompt = copy.deepcopy(current["lanes"])
    long_prompt["daily"]["prompt"] = "字" * (lane_settings.MAX_PROMPT + 1)
    with pytest.raises(lane_settings.SettingsError):
        lane_settings.save(settings, {"revision": current["revision"], "lanes": long_prompt})
    custom = copy.deepcopy(current["lanes"])
    custom["daily"]["prompt"] = "  重点看 ACOS 超过 40% 的店铺  "
    saved = lane_settings.save(settings, {"revision": current["revision"], "lanes": custom})
    assert saved["lanes"]["daily"]["prompt"] == "重点看 ACOS 超过 40% 的店铺"


def test_a_test_send_queues_marked_copies_outside_the_lane_schedule(settings, session_factory):
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    enable(settings, daily={"enabled": False, "to_group": False})  # a test is the owner's explicit choice
    values = cards.report_cards("广告日报 · 2026-11-22", "**全公司核心指标**\n正文")
    later = SEND + timedelta(hours=6)  # 14:30 Beijing, after the 08:30–12:00 window of the daily report
    with session_factory() as db:
        result = lanes.queue_test(db, settings, "daily", values, now=later, preview_id="job-1",
                                  stores={"102": "Campx"}, to_group=True, to_lead=True, mention=True)
        db.commit()
        assert result["status"] == "QUEUED" and result["mentions"] == 1
        assert routes(db) == [("jx_daily_group", GROUP_CHAT, ["ou_op2"]), ("jx_daily_lead", LEAD, [])]
        group, lead = rows(db)
        assert group.idempotency_key.startswith("jx:test:daily:job-1:") and group.payload["jx_test"] is True
        assert group.payload["card"]["header"]["title"]["content"] == "【测试】广告日报 · 2026-11-22"
        assert group.payload["card"]["body"] == values[0]["body"]  # only the title says it is a test
        again = lanes.queue_test(db, settings, "daily", values, now=later, preview_id="job-1",
                                 stores={"102": "Campx"}, to_group=True, to_lead=False, mention=False)
        assert again["status"] == "ALREADY_QUEUED" and len(rows(db)) == 2
        receipt = db.scalar(select(SyncJob).where(SyncJob.job_type == "jx_test"))
        assert receipt.metadata_json["lane"] == "daily" and receipt.metadata_json["targets"] == ["group", "lead"]
        assert db.scalar(select(SyncJob.id).where(SyncJob.job_type == "jx_daily")) is None  # not the day's run
    for row in (group, lead):
        assert lanes.delivery_window(row.idempotency_key, row.payload, later + timedelta(minutes=29))[0] == "READY"
        assert lanes.delivery_window(row.idempotency_key, row.payload, later + timedelta(minutes=31))[0] == "EXPIRED"
        assert lanes.delivery_blocked(settings, row.idempotency_key, row.payload, later) is None
    assert lanes.mention_plan(settings, group.payload) == (["ou_op2"], {"ou_op2": "Campx"})
    assert lanes.mention_plan(settings, lead.payload) == ([], {})
    forged = {**group.payload, "jx_test": False}
    assert lanes.delivery_window(group.idempotency_key, forged, later)[0] == "EXPIRED"  # not a daily key


def test_a_hourly_test_send_never_counts_as_an_alert(settings, session_factory, routing):
    value = cards.hourly_card(HOURLY_FINDINGS, day="2026-11-23", time="22:05", names={}, operators={})
    with session_factory() as db:
        queued = lanes.queue_test(db, settings, "hourly", [value], now=HOUR, preview_id="job-2", stores={},
                                  to_group=True, to_lead=False, mention=True)
        db.commit()
        assert queued["status"] == "QUEUED" and queued["mentions"] == 0
        assert lanes.alerted_today(db, "2026-11-23") == set()
        assert lanes.run_hourly(db, settings, now=HOUR, runner=runner_for())["status"] == "QUEUED"
        changed = copy.deepcopy(routing)
        changed.pop("ad_alert_subscribers")
        write_private(settings_path(settings), changed)
        missing = lanes.queue_test(db, settings, "hourly", [value], now=HOUR, preview_id="job-3", stores={},
                                   to_group=True, to_lead=True, mention=False)
        assert missing == {"status": "NO_RECIPIENT", "missing": ["lead"]}  # nothing is queued half-way
        assert not [row for row in rows(db) if "job-3" in row.idempotency_key]


def two_leads(settings, routing):
    """A second 总负责人 and a second 广告负责人 (both report-group members), as the console stores them."""
    changed = copy.deepcopy(routing)
    changed["company_executive_digest"]["more_recipients"] = [
        {"open_id": "ou_op1", "name": "皮泽霖", "generation": "d" * 32}]
    changed["ad_alert_subscribers"]["more_recipients"] = [{"open_id": "ou_op2", "name": "高爽", "generation": "e" * 32}]
    write_private(settings_path(settings), changed)
    return changed


def test_every_lead_gets_the_message_and_its_excel(settings, session_factory, routing):
    """Owner (Nov 2026): 总负责人 and 广告负责人 can be several people; each one gets a private copy."""
    changed = two_leads(settings, routing)
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for())
        lanes.run_hourly(db, settings, now=HOUR, runner=runner_for())
        db.commit()
        assert routes(db) == [("jx_daily_group", GROUP_CHAT, []), ("jx_daily_lead", LEAD, []),
                              ("jx_daily_lead", "ou_op1", []), ("jx_hourly_group", GROUP_CHAT, []),
                              ("jx_hourly_lead", AD_LEAD, []), ("jx_hourly_lead", "ou_op2", [])]
        # The Excel copy goes exactly where its message goes.
        assert [(role, chat) for role, chat, _ in files(db)] == [(role, chat) for role, chat, _ in routes(db)]
        hourly_files = {name for role, _, name in files(db) if role.startswith("jx_hourly")}
        assert hourly_files == {"广告实时监控_2026-11-23_2205_美西.xlsx"}
        second = next(row for row in rows(db) if row.chat_id == "ou_op1")
    assert lanes.delivery_blocked(settings, second.idempotency_key, second.payload, SEND) is None
    changed["company_executive_digest"].pop("more_recipients")  # the owner removed the second lead
    write_private(settings_path(settings), changed)
    assert "负责人已变更" in lanes.delivery_blocked(settings, second.idempotency_key, second.payload, SEND)


def test_operator_copies_of_the_hourly_monitor_carry_their_own_excel(settings, session_factory):
    enable(settings, hourly={"to_operators": True})
    map_operators(settings, {"102": ("ou_op2", "高爽")})
    with session_factory() as db:
        lanes.run_hourly(db, settings, now=HOUR, runner=runner_for())
        db.commit()
        assert files(db) == [("jx_hourly_group", GROUP_CHAT, "广告实时监控_2026-11-23_2205_美西.xlsx"),
                             ("jx_hourly_lead", AD_LEAD, "广告实时监控_2026-11-23_2205_美西.xlsx"),
                             ("jx_hourly_operator", "ou_op2", "广告实时监控_2026-11-23_2205_美西_高爽.xlsx")]
        receipt = db.scalar(select(SyncJob).where(SyncJob.job_type == "jx_hourly"))
        assert receipt.metadata_json["files"] == ["广告实时监控_2026-11-23_2205_美西.xlsx",
                                                  "广告实时监控_2026-11-23_2205_美西_高爽.xlsx"]
        own = rows(db, excel=True)[2]
    from jx_alert.reports import archive
    with zipfile.ZipFile(io.BytesIO(archive.read(settings, own.payload["file"]))) as book:
        strings = book.read("xl/sharedStrings.xml").decode()
    assert "Campaign 9001" in strings and "Campaign 9002" not in strings  # only 高爽's store


def test_excel_copies_are_dated_and_hold_the_report_tables(settings, session_factory):
    from jx_alert.reports import archive
    with session_factory() as db:
        lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for())
        db.commit()
        row = rows(db, excel=True)[0]
    meta = row.payload["file"]
    assert meta["path"] == "daily/2026-11/广告日报_2026-11-22.xlsx" and len(meta["sha256"]) == 64
    assert row.payload["text"] == "Excel：广告日报_2026-11-22.xlsx" and "card" not in row.payload
    with zipfile.ZipFile(io.BytesIO(archive.read(settings, meta))) as book:
        workbook_xml = book.read("xl/workbook.xml").decode()
        strings = re.findall(r"<t xml:space=\"preserve\">([^<]*)</t>", book.read("xl/sharedStrings.xml").decode())
        detail = book.read("xl/worksheets/sheet3.xml").decode()
        styles = book.read("xl/styles.xml").decode()

    def cell(reference):
        found = re.search(rf'<c r="{reference}"[^>]*?(t="s")?><v>([^<]+)</v>', detail)
        return strings[int(found.group(2))] if found.group(1) else found.group(2)

    assert re.findall(r'<sheet name="([^"]+)"', workbook_xml) == ["摘要", "全公司核心指标", "有花费店铺明细"]
    assert {"统计日（美西）：2026-11-22", "对比日：2026-11-21"} <= set(strings)
    assert any(text.startswith("生成时间（北京）：2026-11-24 08:30") for text in strings)
    # Store table: the date first (sheets of many days stack), 运营 last as on the card, money as numbers.
    assert [cell(f"{column}1") for column in "ABCK"] == ["统计日（美西）", "店铺", "广告花费", "运营"]
    assert cell("A2") == "2026-11-22" and cell("B2") == "香港川页" and cell("C2") == "3217.7"
    assert 'autoFilter ref="A1:K3"' in detail and "&quot;$&quot;#,##0.00" in styles


def test_a_message_without_its_excel_still_goes_out(settings, session_factory, monkeypatch):
    from jx_alert.reports import archive

    def broken(*args, **kwargs):
        raise archive.ArchiveError("disk full")

    monkeypatch.setattr(archive, "save", broken)
    with session_factory() as db:
        assert lanes.queue_morning_lane(db, settings, "daily", now=SEND, runner=runner_for())["status"] == "QUEUED"
        db.commit()
        assert len(rows(db)) == 2 and rows(db, excel=True) == []


def test_a_test_send_reaches_every_lead_with_its_excel(settings, session_factory, routing):
    two_leads(settings, routing)
    values = cards.report_cards("广告日报 · 2026-11-22", "**全公司核心指标**\n正文")
    book = lanes.report_book("daily", lane_result("daily"), "**全公司核心指标**\n正文", SEND, title="广告日报 · 2026-11-22")
    with session_factory() as db:
        result = lanes.queue_test(db, settings, "daily", values, now=SEND, preview_id="job-9", stores={},
                                  to_group=False, to_lead=True, mention=False, book=book)
        db.commit()
        assert result["status"] == "QUEUED" and result["cards"] == 2 and result["files"] == 2
        assert routes(db) == [("jx_daily_lead", LEAD, []), ("jx_daily_lead", "ou_op1", [])]
        assert files(db) == [("jx_daily_lead", LEAD, "【测试】广告日报_2026-11-22.xlsx"),
                             ("jx_daily_lead", "ou_op1", "【测试】广告日报_2026-11-22.xlsx")]
        copy_row = rows(db, excel=True)[0]
        assert copy_row.payload["file"]["path"].startswith("tests/") and copy_row.payload["jx_test"] is True
        assert copy_row.idempotency_key.startswith("jx:test:daily:job-9:") and copy_row.idempotency_key.endswith(":file")
    assert lanes.delivery_window(copy_row.idempotency_key, copy_row.payload, SEND)[0] == "READY"


def test_a_file_from_the_previous_release_loads_with_new_defaults(settings):
    legacy = {lane: {key: value for key, value in values.items() if key not in {"mention_operators", "prompt"}}
              for lane, values in lane_settings.DEFAULTS.items()}
    legacy["daily"]["ai"] = True  # the retired rule-text switch
    write_private(settings_path(settings).with_name("jx-notifications.json"),
                  {"version": 1, "revision": "2" * 64, "updated_at": "2026-11-24T00:00:00+00:00", "lanes": legacy})
    loaded = lane_settings.load(settings)["lanes"]
    assert loaded["review"]["mention_operators"] is True and loaded["daily"]["prompt"] == ""
    assert "ai" not in loaded["daily"]
