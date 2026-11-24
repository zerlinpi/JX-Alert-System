"""The DeepSeek ↔ SparkX MCP agent loop: what is offered, what comes back, and its limits."""
from datetime import datetime, timezone
import json

import anyio

from jx_alert.ai import agent
from jx_alert.sparkx import client as sparkx_client
from jx_alert.sparkx import stores
from sparkx_fake import FakeSparkX, ScriptedDeepSeek, TOOLS, install, reply

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)


def run(settings, client, messages, *, token="synthetic-sparkx-token", **options):
    async def conversation():
        return await agent.run_task(settings, client, token=token, messages_for=lambda context: list(messages),
                                    now=NOW, directory=stores.load(settings), need_roster=False,
                                    max_tool_calls=options.pop("calls", 5), max_rounds=options.pop("rounds", 6),
                                    **options)
    return anyio.run(conversation)


def test_only_allow_listed_read_only_tools_are_offered():
    offered = [item.name for item in TOOLS if sparkx_client.read_only(item)]
    assert offered == ["get_user_authorized_context", "get_ads_perf", "get_entity_metadata", "get_operation_log"]
    # batch_update_ads has no annotations, create_portfolio is not read-only, list_skills is a platform tool.


def test_tool_results_reach_the_model_verbatim_and_unknown_tools_are_refused(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(rows={"campaign": [{"campaignId": 1, "Spend": 3.5}]}), agent)
    client = ScriptedDeepSeek([reply(calls=[("get_ads_perf", {"factEntity": "campaign"}), ("batch_update_ads", {})]),
                               reply("花费 $3.50。")])
    summary, _ = run(settings, client, [{"role": "user", "content": "花了多少"}])
    assert summary["content"] == "花费 $3.50。" and summary["sparkx_calls"] == 1
    tool_messages = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    returned = json.loads(tool_messages[0]["content"])
    assert returned["data"]["rows"] == [{"campaignId": 1, "Spend": 3.5}] and returned["meta"]["hint"]
    assert "不可用" in tool_messages[1]["content"] and [name for name, _ in fake.calls] == ["get_ads_perf"]


def test_the_tool_budget_ends_with_a_final_answer(monkeypatch, settings):
    install(monkeypatch, FakeSparkX(), agent)
    loop = reply(calls=[("get_ads_perf", {"factEntity": "campaign"}), ("get_ads_perf", {"factEntity": "asin"})])
    client = ScriptedDeepSeek([loop, reply("根据已有结果：……")])
    summary, _ = run(settings, client, [{"role": "user", "content": "查"}], calls=1)
    assert summary["sparkx_calls"] == 1 and client.requests[-1]["tool_choice"] == "none"
    budget = [m for m in client.requests[-1]["messages"] if m["role"] == "tool"][1]["content"]
    assert "次数已用完" in budget


def test_the_store_roster_reads_the_live_envelope():
    profiles = anyio.run(sparkx_client.authorized_profiles, FakeSparkX())
    assert len(profiles) == 7 and profiles["101"] == {"name": "香港川页", "country": "US", "store_type": "seller",
                                                      "currency": "USD", "timezone": "America/Los_Angeles"}


def test_long_results_are_cut_with_a_note():
    text = agent.clip("x" * (agent.MAX_RESULT_CHARS + 10))
    assert len(text) < agent.MAX_RESULT_CHARS + 200 and "结果过长" in text
    assert agent.clip("short") == "short"


def test_the_catalog_is_cached_so_chat_needs_no_sparkx_session(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(), agent)
    run(settings, ScriptedDeepSeek([reply("你好！")]), [{"role": "user", "content": "你好"}])
    assert fake.listed == 1 and len(fake.opened) == 1
    run(settings, ScriptedDeepSeek([reply("在的。")]), [{"role": "user", "content": "在吗"}])
    assert fake.listed == 1 and len(fake.opened) == 1  # cached catalog, no session for pure conversation


def test_an_unreachable_sparkx_leaves_chat_without_data_tools(monkeypatch, settings):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def broken(token, timeout, **_):
        raise sparkx_client.SparkXError("SPARKX_AUTH_REJECTED")
        yield  # pragma: no cover

    monkeypatch.setattr(agent, "open_session", broken)
    client = ScriptedDeepSeek([reply("现在查不了数据，但可以聊。")])
    summary, context = run(settings, client, [{"role": "user", "content": "你好"}], optional_sparkx=True)
    assert summary["content"].startswith("现在查不了") and context["sparkx_unavailable"]
    assert client.requests[0]["tools"] == []


def test_system_prompt_names_tools_time_and_directory(settings):
    roster = {"101": {"name": "香港川页", "country": "US", "store_type": "seller"}}
    text = agent.system_prompt(now=NOW, tool_names=["get_ads_perf"], skills_index=None, roster=roster,
                               directory=stores.load(settings), mode="chat")
    assert "北京时间 2026-11-24 11:00" in text and "美西（America/Los_Angeles）2026-11-23 19:00" in text
    assert "get_ads_perf" in text and "| 101 | 香港川页 |" in text and "未映射运营" in text
    assert "自由交流" in text and "无法修改" in text
