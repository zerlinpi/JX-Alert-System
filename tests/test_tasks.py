"""Lane tasks: the fixed scope around the owner's text, and checks on DeepSeek's answers."""
import json

import pytest

from jx_alert.ai import agent
from jx_alert.reports import settings as lane_settings
from jx_alert.reports import tasks
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores
from jx_alert.sparkx.client import SparkXError
from lane_fakes import HOUR, SEND
from sparkx_fake import FakeSparkX, ScriptedDeepSeek, install, reply

HOURLY = lane_settings.DEFAULTS["hourly"]
ROWS = [{"profileId": 102, "campaignId": 9001, "campaignName": "SP-Auto", "Spend": 41.2, "Clicks": 28, "Conversions": 0},
        {"profileId": 101, "campaignId": 9002, "campaignName": "SP-Exact", "Spend": 12.5, "Clicks": 19, "Conversions": 0}]


@pytest.fixture(autouse=True)
def no_skill_download(monkeypatch):
    monkeypatch.setattr(skill_store, "ensure", lambda settings: None)


def test_daily_task_fixes_dates_scope_and_contract_around_the_owner_text():
    text = tasks.task_message("daily", {"prompt": ""}, now=SEND, monitored={"101", "102", "201"})
    assert "统计日：2026-11-22（美西已结束的完整日）；对比日：2026-11-21" in text
    assert "profileIds = [101, 102, 201]" in text and "不要包含其他店铺" in text
    assert tasks.DEFAULT_PROMPTS["daily"] in text and "TACOS" in text
    assert "把「运营」放在最后一列" in text and "JX_STORES" in text and "须知" in text
    assert "花费日环比 | 运营」" in tasks.DEFAULT_PROMPTS["daily"] and "主要问题 | 运营」" in tasks.DEFAULT_PROMPTS["review"]
    custom = tasks.task_message("daily", {"prompt": "只看 ACOS"}, now=SEND, monitored={"101"})
    assert custom.endswith("只看 ACOS") and tasks.DEFAULT_PROMPTS["daily"] not in custom
    review = tasks.task_message("review", {"prompt": ""}, now=SEND, monitored={"101"})
    assert "近 7 日：2026-11-16 ~ 2026-11-22；前 7 日：2026-11-09 ~ 2026-11-15" in review and "JX_STORES" in review


def test_hourly_task_carries_the_thresholds_and_what_was_already_alerted():
    text = tasks.task_message("hourly", HOURLY, now=HOUR, monitored={"101"}, alerted={"101:9:low_spend_no_conversion"})
    assert "美西今天：2026-11-23，当前美西时间 22:05" in text
    assert "花费 ≥ $30 且订单 = 0" in text and "点击 ≥ 15" in text and "ACOS ≤ 20%" in text
    assert "101:9:low_spend_no_conversion" in text and '"findings"' in text


def test_the_review_store_line_is_removed_and_limited_to_monitored_stores():
    text, ids = tasks.split_stores("复盘正文\nJX_STORES: 101, 999,102", {"101", "102"})
    assert text == "复盘正文" and ids == ["101", "102"]
    assert tasks.split_stores("复盘\nJX_STORES: 无", {"101"}) == ("复盘", [])
    assert tasks.split_stores("复盘", {"101"}) == ("复盘", None)


def test_json_answers_may_be_fenced():
    assert tasks.parse_json('```json\n{"findings": []}\n```') == {"findings": []}
    assert tasks.parse_json('结果如下：{"findings": [], "note": "x"}') == {"findings": [], "note": "x"}
    with pytest.raises(ValueError):
        tasks.parse_json("没有 JSON")


def test_hourly_findings_must_be_traceable_consistent_and_new():
    summary = {"evidence": [json.dumps(ROWS)]}
    value = {"findings": [
        {"profile_id": 102, "campaign_id": "9001", "campaign_name": "SP-Auto", "category": "high_spend_no_conversion",
         "spend": 41.2, "clicks": 28, "orders": 0, "sales": 0, "acos": None, "cvr": 0, "suggestion": "降价"},
        {"profile_id": 101, "campaign_id": "7777", "category": "high_spend_no_conversion", "spend": 60, "clicks": 5,
         "orders": 0},                                                   # never returned by SparkX
        {"profile_id": 101, "campaign_id": "9002", "category": "high_spend_no_conversion", "spend": 12.5, "clicks": 19,
         "orders": 0},                                                   # $12.50 is not high spend
        {"profile_id": 999, "campaign_id": "9001", "category": "high_spend_no_conversion", "spend": 41.2, "clicks": 28,
         "orders": 0},                                                   # store not monitored
        {"profile_id": 101, "campaign_id": "9002", "category": "low_spend_no_conversion", "spend": 12.5, "clicks": 19,
         "orders": 0}]}
    kept, dropped = tasks.hourly_findings(value, summary, monitored={"101", "102"}, config=HOURLY,
                                          alerted={"101:9002:low_spend_no_conversion"})
    assert [(row["pid"], row["campaign_id"], row["category"]) for row in kept] == [
        ("102", "9001", "high_spend_no_conversion")]
    assert dropped == 3 and kept[0]["campaign"] == "SP-Auto"
    with pytest.raises(ValueError):
        tasks.hourly_findings({"findings": "none"}, summary, monitored={"101"}, config=HOURLY, alerted=set())


def test_daily_run_lets_deepseek_query_sparkx_with_the_servers_own_tools(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(rows={"campaign": ROWS}), agent)
    stores.save(settings, {"105": {"monitored": False, "operators": []},
                           "101": {"monitored": True, "operators": [{"open_id": "ou_op1", "name": "皮泽霖"}]}},
                stores.load(settings)["revision"])
    query = {"profileIds": [101, 102], "factEntity": "campaign", "dateStart": "2026-11-22", "dateEnd": "2026-11-22",
             "metrics": ["Spend"], "userContext": "JX 日报"}
    client = ScriptedDeepSeek([reply(calls=[("get_ads_perf", query)], reasoning="先查花费"),
                               reply("**全公司核心指标**\n| 指标 | 当日 |\n|---|---|\n| 广告花费 | $53.70 |\nJX_STORES: 101")])
    rosters = []
    result = tasks.run_lane(settings, "daily", lane_settings.DEFAULTS["daily"], now=SEND,
                            directory=stores.load(settings), on_roster=rosters.append, client_factory=lambda s: client)
    assert result["content"].startswith("**全公司核心指标**") and result["report_date"] == "2026-11-22"
    assert result["stores"] == ["101"] and "JX_STORES" not in result["content"]
    assert "105" not in result["profiles"] and result["summary"]["sparkx_calls"] == 1 and rosters
    first = client.requests[0]
    assert first["tools"] == ["get_user_authorized_context", "get_ads_perf", "get_entity_metadata", "get_operation_log"]
    spec = next(item for item in first["specs"] if item["function"]["name"] == "get_ads_perf")["function"]
    assert spec["description"] == "Query Amazon advertising performance." and "$schema" not in spec["parameters"]
    system, task = first["messages"][0]["content"], first["messages"][1]["content"]
    assert "皮泽霖" in system and "| 105 |" not in system  # only stores in the notifications are listed
    assert "profileIds = [101, 102, 103, 104, 106, 201]" in task
    second = client.requests[1]["messages"]
    assert second[2]["reasoning_content"] == "先查花费"  # thinking mode: reasoning is passed back
    tool_reply = second[3]
    assert tool_reply["role"] == "tool" and json.loads(tool_reply["content"])["data"]["rows"] == ROWS  # verbatim
    assert fake.calls[-1] == ("get_ads_perf", query)


def test_a_daily_report_without_the_store_line_asks_which_stores_it_flags(monkeypatch, settings):
    install(monkeypatch, FakeSparkX(rows={"campaign": ROWS}), agent)
    client = ScriptedDeepSeek([reply("**须知**\n1. Campx 有花费没出单。")],
                              json_replies=[{"profile_ids": ["102", "999"]}])
    result = tasks.run_lane(settings, "daily", lane_settings.DEFAULTS["daily"], now=SEND,
                            directory=stores.load(settings), client_factory=lambda s: client)
    assert result["stores"] == ["102"]  # only stores in the notifications
    assert "须知" in client.requests[-1]["system"] and client.requests[-1]["json"] is True


def test_hourly_run_drops_rows_that_sparkx_never_returned(monkeypatch, settings):
    install(monkeypatch, FakeSparkX(rows={"campaign": ROWS}), agent)
    answer = {"marketplace_date": "2026-11-23", "findings": [
        {"profile_id": 102, "campaign_id": "9001", "campaign_name": "SP-Auto", "category": "high_spend_no_conversion",
         "spend": 41.2, "clicks": 28, "orders": 0, "acos": None, "cvr": 0, "suggestion": "否定无关搜索词"},
        {"profile_id": 101, "campaign_id": "4242", "category": "high_spend_no_conversion", "spend": 90, "clicks": 40,
         "orders": 0}]}
    client = ScriptedDeepSeek([reply(calls=[("get_ads_perf", {"factEntity": "campaign"})]),
                               reply("```json\n" + json.dumps(answer) + "\n```")])
    result = tasks.run_lane(settings, "hourly", HOURLY, now=HOUR, directory=stores.load(settings),
                            client_factory=lambda s: client)
    assert [row["campaign_id"] for row in result["findings"]] == ["9001"] and result["dropped"] == 1
    assert result["day"] == "2026-11-23" and result["time"] == "22:05"


def test_a_run_without_token_or_stores_fails_safely(monkeypatch, settings):
    blank = settings.model_copy(update={"sparkx_ai_token": ""})
    with pytest.raises(SparkXError) as exc:
        tasks.run_lane(blank, "daily", lane_settings.DEFAULTS["daily"], now=SEND, directory=stores.load(settings),
                       client_factory=lambda s: ScriptedDeepSeek([]))
    assert exc.value.code == "SPARKX_NOT_CONFIGURED"
    install(monkeypatch, FakeSparkX(profiles={}), agent)
    with pytest.raises(SparkXError) as exc:
        tasks.run_lane(settings, "daily", lane_settings.DEFAULTS["daily"], now=SEND, directory=stores.load(settings),
                       client_factory=lambda s: ScriptedDeepSeek([]))
    assert exc.value.code == "SPARKX_NO_PROFILES"
