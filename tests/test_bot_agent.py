"""The Feishu bot's DeepSeek agent: free conversation, SparkX only when needed, context from history."""
from datetime import datetime, timezone

from jx_alert import bot_agent
from jx_alert.ai import agent
from jx_alert.sparkx import skills as skill_store
from sparkx_fake import FakeSparkX, ScriptedDeepSeek, install, reply

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)


def ask(settings, client, question, **kwargs):
    return bot_agent.answer_question(settings, question, now=NOW, client_factory=lambda s: client, **kwargs)


def test_small_talk_is_answered_without_sparkx_queries(monkeypatch, settings):
    monkeypatch.setattr(skill_store, "ensure", lambda settings: None)
    fake = install(monkeypatch, FakeSparkX(), agent)
    client = ScriptedDeepSeek([reply("你好！有什么可以帮你？")])
    result = ask(settings, client, "你好")
    assert result == {"answer": "你好！有什么可以帮你？", "provider": "deepseek", "calls": 0, "skill_reads": 0,
                      "usage": client.last_usage, "writes": 0, "write": None}
    assert fake.calls == []  # only the tool list was read
    system = client.requests[0]["messages"][0]["content"]
    assert "自由交流" in system and "get_ads_perf" in system


def test_follow_ups_carry_the_conversation_and_data_comes_from_sparkx(monkeypatch, settings):
    monkeypatch.setattr(skill_store, "ensure", lambda settings: None)
    fake = install(monkeypatch, FakeSparkX(rows={"campaign": [{"campaignId": 7, "Spend": 9.9}]}), agent)
    client = ScriptedDeepSeek([reply(calls=[("get_ads_perf", {"factEntity": "campaign"})]), reply("上周花了 $9.90。")])
    history = [{"question": "昨天花了多少", "answer": "昨天花了 $5.00。"}]
    result = ask(settings, client, "那上周呢", history=history)
    assert result["answer"] == "上周花了 $9.90。" and result["calls"] == 1
    messages = client.requests[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "昨天花了多少" and messages[3]["content"] == "那上周呢"
    assert [name for name, _ in fake.calls] == ["get_ads_perf"]


def test_without_sparkx_the_bot_still_chats(monkeypatch, settings):
    blank = settings.model_copy(update={"sparkx_ai_token": ""})
    client = ScriptedDeepSeek([reply("目前没有接入 SparkX，暂时查不了数据。")])
    result = ask(blank, client, "昨天花费多少")
    assert result["calls"] == 0 and client.requests[0]["tools"] == []
    assert "SparkX 未配置" in client.requests[0]["messages"][0]["content"]
