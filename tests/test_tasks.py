"""Lane tasks: the fixed scope around the owner's text, and checks on DeepSeek's answers."""
import json
import re

import pytest

from jx_alert.ai import agent
from jx_alert.reports import cards, figures
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
    for message in (custom, review):  # fixed contract: also when the owner rewrote the task text
        assert "广告订单/总订单、广告销量/总销量" in message and "不要只写「订单」「销量」「销售额」" in message


def _table_spec(text, section):
    """The columns a section of the task text asks for, e.g. 「店铺 | … | 运营」."""
    found = re.search(re.escape(section) + r"[^「]*「([^」]+)」", text)
    return [name.strip() for name in found.group(1).split("|")]


def test_the_daily_report_tells_ad_and_store_totals_apart():
    """Owner (Nov 2026): 总销量 is shown, and 广告订单/总订单, 广告销量/总销量 are never mixed up."""
    daily = tasks.DEFAULT_PROMPTS["daily"]
    stores_table = _table_spec(daily, "**有花费店铺明细**")
    # Owner (Nov 26 2026): the figures read first come right after the store name.
    assert stores_table == ["店铺", "广告花费", "ACOS", "TACOS", "CPC", "ROAS", "CVR", "广告销售额", "总销售额",
                            "广告订单", "总订单", "广告销量", "总销量", "花费日环比", "运营"]
    assert stores_table[1:7] == list(figures.LEADING)  # the same order the system enforces
    core = re.search(r"依次为([^。]+)。", daily).group(1).split("、")
    assert core[:7] == ["总销售额", "广告销售额", "总订单", "广告订单", "总销量", "广告销量", "广告花费"]
    site = _table_spec(daily, "**站点汇总**")
    assert {"广告订单", "总订单", "广告销量", "总销量"} <= set(site) and not {"订单", "销量", "销售额"} & set(site)
    assert max(len(stores_table), len(site)) <= cards.MAX_COLUMNS
    # Where each figure comes from (SparkX sparkx-query-ads-performance field reference, checked live).
    assert "factEntity=campaign 的 Spend、Sales、Conversions、Units、Clicks" in daily
    assert "factEntity=asin 的 TotalSalesAmount、OrderCount、UnitCount" in daily
    # Live data: the latest day's OrderCount can still be 0 while UnitCount is filled; that is missing, not zero.
    assert "总订单为 0 但总销量大于 0" in daily and "写「—」，不当成 0" in daily
    # A store without store totals must not inflate TACOS: its ad spend stays out of the numerator too.
    assert "全公司 TACOS = 有总销售额数据的店铺的广告花费合计 ÷ 这些店铺的总销售额合计" in daily
    assert "不超过 60 字" in daily and "CPC 写两位小数的金额" in daily


def test_reports_carry_only_sentences_with_information():
    """Owner (Nov 26 2026): no 「对比 …，监控 19 家店铺…；金额已统一折算为 USD，…为暂定值…」, no 「今日数据仍在累计…」."""
    daily, review = tasks.DEFAULT_PROMPTS["daily"], tasks.DEFAULT_PROMPTS["review"]
    for old in ("开头一句话", "注明金额为 USD", "用半句话注明", "在开头注明一次", "「暂无广告数据：…」", "N 个店铺中，M 个有投放数据"):
        assert old not in daily + review, old
    assert "前面不写任何概述" in daily and "表格下方不写说明或店铺名单" in daily and "没有发现的问题不写" in daily
    assert "开头不写店铺总数、有投放数据的店铺数、币种、暂定值" in review and "没有检出的类型整段不写" in review
    assert "「K 个店铺检出 X 个异常 campaign，合计浪费约 $Y。」" in review
    for lane in ("daily", "review"):
        message = tasks.task_message(lane, {"prompt": "自定义说明"}, now=SEND, monitored={"101"})
        assert "每句话都要有信息量" in message and "不写统计范围、监控或有数据的店铺数、币种" in message
        assert "照写，但只用一句短话" not in message  # the old rule asked for the notes
    assert "一律不写" in agent.PRIORITY and "要求向读者说明的事项（例如" not in agent.PRIORITY
    chat = agent.system_prompt(now=SEND, tool_names=[], skills_index=None, roster={}, directory={"profiles": {}},
                               mode="chat")
    assert "不写币种、暂定值、数据仍在累计之类的提醒" in chat and "一句话带过" not in chat


def test_the_review_judges_anomalies_by_the_products_own_history_and_value():
    """Owner (Nov 2026): ad anomalies are measured against the product's past data, its value and its type."""
    review = tasks.DEFAULT_PROMPTS["review"]
    # The product behind each candidate campaign and its own baseline, from SparkX (Skill field names).
    assert "factEntity=productAd" in review and "productAd.asin_" in review and "基准期（前 7 日之前的 30 天）" in review
    assert all(field in review for field in ("asin.asinPrice_", "asin.asinCategoryInfo_", "asin.asinOpenDate_"))
    # Value: the Skill's T (one order's allowed spend) instead of a fixed $50; history and type adjust the verdict.
    assert "T = 客单价 × 目标 ACOS" in review and "≥ $50" not in review and "样本不足" in review
    assert "新品期" in review and "高客单" in review and "浪费 = 花费 − 广告订单 × 产品历史 CPA" in review
    # Campaigns in an AI managed group get group-level advice, never bid numbers (Skill rule).
    assert "campaign.aiGroupId_" in review and "不给出价数字" in review
    columns = [name.strip() for name in re.search(r"表格「(店铺 \| Campaign[^」]+)」", review).group(1).split("|")]
    assert {"主推产品", "客单价", "产品历史"} <= set(columns) and columns[-1] == "运营"
    assert len(columns) <= cards.MAX_COLUMNS


def test_sparkx_data_and_skills_rank_above_the_jx_task_text():
    """Owner (Nov 2026): SparkX's data and its official Skills first, JX's own instructions second."""
    for lane in ("daily", "review", "hourly"):
        config = {**lane_settings.DEFAULTS[lane], "prompt": "自定义说明"}
        message = tasks.task_message(lane, config, now=SEND, monitored={"101"})
        assert tasks.TASK_LABEL + "\n自定义说明" in message  # the owner's text is labelled second priority
    for lane in ("daily", "review"):
        message = tasks.task_message(lane, {"prompt": ""}, now=SEND, monitored={"101"})
        assert "不写数据来源、生成方式、查询次数和查询或分析过程" in message
        assert "不重复标题里已有的报告名称和日期" in message
    assert "不重复判定条件" in tasks.DEFAULT_PROMPTS["review"]  # section titles name the problem type only
    for mode in ("report", "chat"):
        system = agent.system_prompt(now=SEND, tool_names=["get_ads_perf"], skills_index=None, roster={},
                                     directory={"profiles": {}}, mode=mode)
        assert agent.PRIORITY in system
        first, second = system.index("第一优先级"), system.index("第二优先级")
        assert first < second and "冲突时以第 1 条为准" in system


def test_hourly_task_carries_the_thresholds_and_what_was_already_alerted():
    text = tasks.task_message("hourly", HOURLY, now=HOUR, monitored={"101"}, alerted={"101:9:low_spend_no_conversion"})
    assert "美西今天：2026-11-23，当前美西时间 22:05" in text
    assert "花费 ≥ $30 且广告订单 = 0" in text and "点击 ≥ 15" in text and "ACOS ≤ 20%" in text
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


def test_the_system_settles_the_daily_report_from_sparkx_rows(monkeypatch, settings):
    """Owner (Nov 26 2026): no overview, no store lists or data remarks, the lead figures first; and the
    model's own lists were unreliable (Nov 2026), so the one store statement left comes from SparkX's rows."""
    ads = [{"profileId": 101, "date": "20261122", "Spend": 300.0, "Sales": 900.0, "Clicks": 200},
           {"profileId": 102, "date": "20261122", "Spend": 41.2, "Sales": 0, "Clicks": 30},
           {"profileId": 106, "date": "20261121", "Spend": 35.3, "Sales": 0}]  # OCTORA stopped spending
    totals = [{"profileId": 101, "date": "20261122", "TotalSalesAmount": 1500.0, "OrderCount": 30, "UnitCount": 32},
              {"profileId": 102, "date": "20261122", "TotalSalesAmount": 27.99, "OrderCount": 1, "UnitCount": 1}]
    install(monkeypatch, FakeSparkX(rows={"campaign": ads, "asin": totals}), agent)
    window = {"profileIds": [101, 102, 103, 104, 105, 106, 201], "dateStart": "2026-11-21", "dateEnd": "2026-11-22",
              "select": ["profile.profileId_", "date"], "userContext": "JX 日报"}
    written = ("对比 2026-11-21，监控 7 家店铺、当日有广告花费 2 家；金额已统一折算为 USD，转化数据为暂定值。\n\n"
               "**有花费店铺明细**\n| 店铺 | 广告花费 | 总销售额 | ROAS | 运营 | ACOS | PPC |\n|---|---|---|---|---|---|---|\n"
               "| 香港川页 | $300.00 | $1,500.00 | 3.00 | 未映射运营 | 33.3% | $1.50 |\n"
               "| Campx | $41.20 | $27.99 | 0.00 | 未映射运营 | — |\n"
               "当日无广告花费：FITFIZZ、陈子俊。\n注：金额为 USD。\n\n**须知**\n1. 花费集中在香港川页。\n"
               "2. Campx 总销售额暂无数据，昌泉也暂无数据。\n3. 有广告花费但无广告订单的店铺：无。\nJX_STORES: 102")
    client = ScriptedDeepSeek([reply(calls=[("get_ads_perf", {**window, "factEntity": "campaign"}),
                                            ("get_ads_perf", {**window, "factEntity": "asin"})]), reply(written)])
    result = tasks.run_lane(settings, "daily", lane_settings.DEFAULTS["daily"], now=SEND,
                            directory=stores.load(settings), client_factory=lambda s: client)
    assert result["content"] == (
        "**有花费店铺明细**\n| 店铺 | 广告花费 | ACOS | PPC | ROAS | 总销售额 | 运营 |\n|---|---|---|---|---|---|---|\n"
        "| 香港川页 | $300.00 | 33.3% | $1.50 | 3.00 | $1,500.00 | 未映射运营 |\n"
        "| Campx | $41.20 | — | — | 0.00 | $27.99 | 未映射运营 |\n"
        "当日无广告花费（前一日有）：OCTORA（前一日 $35.30）。\n\n**须知**\n1. 花费集中在香港川页。")
    # The store that stopped spending is flagged too, so its operator is named and mentioned that day.
    assert result["stores"] == ["102", "106"] and "requests" not in result["summary"] and "evidence" not in result["summary"]


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
