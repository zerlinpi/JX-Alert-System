"""DeepSeek tool loop over read-only SparkX tools, with figure verification."""
from datetime import datetime, timezone

from jx_alert import bot_agent
from sparkx_fake import FakeSparkX, install, metrics

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
EMPTY = {"version": 1, "revision": "0" * 64, "profiles": {
    "105": {"monitored": False, "operators": []},
    "101": {"monitored": True, "operators": [{"open_id": "ou_op1", "name": "皮泽霖"}]}}}
ROWS = {("campaign", "2026-11-22", "2026-11-22"): [
    {"profileId": 101, "campaignName": "Brand-Exact", **metrics(5000, 200, 300.0, 1200.0, 20)},
    {"profileId": 101, "campaignName": "Auto", **metrics(3000, 100, 150.0, 0.0, 0)}],
    ("campaign", "2026-11-21", "2026-11-21"): [
    {"profileId": 101, "campaignName": "Brand-Exact", **metrics(4000, 180, 240.0, 1000.0, 18)}]}


def call(name, arguments, index=1):
    return {"id": f"call_{index}", "name": name, "arguments": arguments}


class Model:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, *, tools=None, **_):
        self.seen.append([dict(message) for message in messages])
        reply = self.replies.pop(0)
        calls = reply.get("tool_calls", [])
        message = {"role": "assistant", "content": reply.get("content", "")}
        if calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": "{}"}}
                                     for c in calls]
        return {"content": reply.get("content", ""), "tool_calls": calls, "finish_reason": "stop", "message": message}


QUERY = {"profile_ids": [101], "fact_entity": "campaign", "date_start": "2026-11-22", "date_end": "2026-11-22",
         "metrics": ["Spend", "Sales", "Clicks", "Conversions"], "dimensions": ["campaign.campaignName_"],
         "compare_previous": True}


def test_model_lists_stores_queries_and_answers_with_verified_figures(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(ROWS), bot_agent)
    model = Model({"tool_calls": [call("list_stores", {})]},
                  {"tool_calls": [call("query_ads_performance", QUERY, 2)]},
                  {"content": "香港川页昨天花费 $300.0 的 Brand-Exact ACOS 25.0%，较前一日花费 +25.0%；Auto 花费 150.0 没有出单。"})
    result = bot_agent.answer_question(settings, "川页昨天哪个活动最费钱", now=NOW, client_factory=lambda s: model,
                                       directory=EMPTY)
    assert result["provider"] == "deepseek" and result["verified"] and result["calls"] == 2
    stores = [message for message in model.seen[1] if message["role"] == "tool"][0]["content"]
    assert "香港川页" in stores and "昌泉" not in stores and "皮泽霖" in stores  # 105 is not monitored
    queries = [args for tool, args in fake.calls if tool == "get_ads_perf"]
    assert [q["dateStart"] for q in queries] == ["2026-11-22", "2026-11-21"]
    assert all(q["profileIds"] == [101] and q["select"] == ["campaign.campaignName_"] for q in queries)
    system = model.seen[0][0]["content"]
    assert "美西今天 2026-11-23" in system and "最近完整日 2026-11-22" in system


def test_unmonitored_profiles_and_write_shapes_are_refused_to_the_model(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(ROWS), bot_agent)
    model = Model({"tool_calls": [call("query_ads_performance", {**QUERY, "profile_ids": [105]})]},
                  {"tool_calls": [call("batch_update_ads", {"campaignId": 1, "budget": 0}, 2)]},
                  {"content": "无法查询该店铺。"})
    result = bot_agent.answer_question(settings, "昌泉怎么样", now=NOW, client_factory=lambda s: model, directory=EMPTY)
    tool_messages = [m["content"] for m in model.seen[-1] if m["role"] == "tool"]
    assert "查询被拒绝" in tool_messages[0] and "未知工具" in tool_messages[1]
    assert [tool for tool, _ in fake.calls] == ["get_user_authorized_context"]
    assert result["provider"] == "deepseek"


def test_invented_numbers_get_one_correction_then_raw_results(monkeypatch, settings):
    install(monkeypatch, FakeSparkX(ROWS), bot_agent)
    model = Model({"tool_calls": [call("query_ads_performance", QUERY)]},
                  {"content": "Brand-Exact 下周花费预计 $999。"},
                  {"content": "Brand-Exact 大约会花 $888。"})
    result = bot_agent.answer_question(settings, "预测一下", now=NOW, client_factory=lambda s: model, directory=EMPTY)
    assert result["provider"] == "unverified" and result["answer"] is None
    assert result["results"][0]["result"]["rows"][0]["campaignName"] == "Brand-Exact"
    correction = model.seen[2][-1]["content"]
    assert "工具结果中没有的数字" in correction and "999" in correction
