"""Ad writes behind the owner's 「广告写入」 switch (owner decision, Nov 2026).

Everything here runs against FakeSparkX: no test calls a SparkX write tool on a live server.
"""
from datetime import timedelta
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import anyio
import pytest
from sqlalchemy import select

from conftest import AD_LEAD, LEAD
from jx_alert import bot_agent, bot_service
from jx_alert.ai import agent
from jx_alert.notifications.cards import plain_text
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.reports import settings as lane_settings
from jx_alert.reports import tasks
from jx_alert.sparkx import client as sparkx_client
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores, write_mode
from jx_alert.sparkx.client import WRITE_TOOLS, SparkXError
from jx_alert.storage.models import SyncJob
from lane_fakes import SEND
from sparkx_fake import TOOLS, FakeSparkX, ScriptedDeepSeek, install, reply, result
from test_bot_service import queue
from test_skills import install_fake

READS = ["get_user_authorized_context", "get_ads_perf", "get_entity_metadata", "get_operation_log"]
PLAN = ("| 店铺 | 活动 | 每日预算 |\n|---|---|---|\n| Campx | SP-Auto（9001） | $30 → $50 |\n\n"
        "回复「确认执行」后我再修改")


# ---------------------------------------------------------------- the switch and its helpers

def test_the_switch_is_off_by_default_and_any_doubt_means_off(settings):
    assert write_mode.load(settings) == {"revision": "0" * 64, "enabled": False, "updated_at": None, "enabled_at": None}
    assert write_mode.enabled(settings) is False
    on = write_mode.save(settings, True, "0" * 64)
    assert on["enabled"] is True and on["enabled_at"] and write_mode.enabled(settings)
    with pytest.raises(write_mode.WriteModeError) as exc:
        write_mode.save(settings, False, "0" * 64)  # another tab saved in between
    assert exc.value.status_code == 409
    with pytest.raises(write_mode.WriteModeError) as exc:
        write_mode.save(settings, "yes", on["revision"])
    assert exc.value.status_code == 400
    path = Path(settings.sparkx_write_file)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("{broken", encoding="utf-8")
    assert write_mode.enabled(settings) is False  # unreadable: off


def test_only_an_exact_confirmation_counts():
    for text in ("确认执行", " 确认 执行 ", "「确认执行」", "确认执行！"):
        assert write_mode.confirms(text), text
    for text in ("确认执行，但预算改成 60", "确认", "执行", "好的", "不要确认执行", "", None):
        assert not write_mode.confirms(text), text


def test_the_confirm_token_never_reaches_a_record():
    request = {"profileId": 101, "request": {"confirmToken": "tok-123", "items": [{"confirm_token": "x"}]}}
    assert write_mode.redact(request) == {"profileId": 101, "request": {"confirmToken": "***",
                                                                        "items": [{"confirm_token": "***"}]}}
    assert request["request"]["confirmToken"] == "tok-123"  # the call itself keeps the token
    text = '{"status": "PENDING_CONFIRMATION", "data": {"confirmToken": "tok-123", "expiresIn": 300}}'
    assert "tok-123" not in write_mode.redact_text(text) and "PENDING_CONFIRMATION" in write_mode.redact_text(text)


def test_write_tools_are_offered_only_in_write_mode():
    def names(write):
        return [item.name for item in TOOLS if sparkx_client.offered(item, write=write)]

    assert names(False) == READS
    # list_skills (a SparkX skill-platform tool) is never offered.
    assert names(True) == [*READS, "batch_update_ads", "create_portfolio", "delete_ai_managed_group"]
    helper = SimpleNamespace(name="get_keyword_list", annotations=SimpleNamespace(readOnlyHint=True))
    unsure = SimpleNamespace(name="get_keyword_list", annotations=SimpleNamespace(readOnlyHint=False))
    assert sparkx_client.offered(helper, write=True) and not sparkx_client.offered(helper)
    assert not sparkx_client.offered(unsure, write=True)  # a helper read runs without the gate: must be read-only
    with pytest.raises(SparkXError):
        anyio.run(sparkx_client.call_raw, FakeSparkX(), "batch_update_ads", {})


# ---------------------------------------------------------------- the confirmation gate

class TwoPhase(FakeSparkX):
    """batch_update_ads as the edit-ads Skill describes it: a preview with a confirmToken, then the change."""

    async def call_tool(self, name, arguments=None):
        if name != "batch_update_ads":
            return await super().call_tool(name, arguments)
        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        if "confirmToken" not in (arguments.get("request") or {}):
            return result({"isError": False, "toolName": name, "status": "PENDING_CONFIRMATION",
                           "data": {"confirmToken": "tok-secret",
                                    "preview": [{"campaignId": 9001, "before": 30, "after": 50}]}})
        return result({"isError": False, "toolName": name, "status": "SUCCESS", "data": {"updated": 1}})


def converse(settings, client, *, gate=None, write=False, calls=6):
    async def conversation():
        return await agent.run_task(settings, client, token="synthetic-sparkx-token",
                                    messages_for=lambda context: [{"role": "user", "content": "把预算改成 50"}],
                                    now=SEND, directory=stores.load(settings), need_roster=False,
                                    max_tool_calls=calls, max_rounds=6, write=write, write_gate=gate)
    return anyio.run(conversation)


def tool_replies(client, index):
    return [message["content"] for message in client.requests[index]["messages"] if message["role"] == "tool"]


def test_a_plan_turn_blocks_every_write_and_records_it(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(), agent)
    records = []
    gate = agent.WriteGate(confirmed=False, record=lambda *args: records.append(args))
    client = ScriptedDeepSeek([reply(calls=[("batch_update_ads", {"profileId": 101})]), reply(PLAN)])
    summary, context = converse(settings, client, gate=gate, write=True)
    assert "batch_update_ads" in client.requests[0]["tools"] and context["write"] is True
    assert fake.calls == []  # nothing reached SparkX
    assert tool_replies(client, 1) == [agent.BLOCKED_WRITE]
    assert records == [(1, "batch_update_ads", {"profileId": 101}, "BLOCKED", None)]
    assert summary["content"] == PLAN and gate.count == 0


def test_a_confirmed_turn_runs_both_phases_without_recording_the_token(monkeypatch, settings):
    fake = install(monkeypatch, TwoPhase(), agent)
    records = []
    gate = agent.WriteGate(confirmed=True, record=lambda *args: records.append(args))
    change = {"campaigns": [{"campaignId": 9001, "dailyBudget": 50}]}
    client = ScriptedDeepSeek([reply(calls=[("batch_update_ads", {"profileId": 101, "request": change})]),
                               reply(calls=[("batch_update_ads", {"profileId": 101,
                                                                  "request": {**change, "confirmToken": "tok-secret"}})]),
                               reply("已把 SP-Auto 每日预算改为 $50。")])
    summary, _ = converse(settings, client, gate=gate, write=True)
    assert [name for name, _ in fake.calls] == ["batch_update_ads", "batch_update_ads"]
    assert fake.calls[1][1]["request"]["confirmToken"] == "tok-secret"  # SparkX gets the token unchanged
    assert "tok-secret" in tool_replies(client, 1)[0]  # the model needs it for the second call
    assert [(attempt, status) for attempt, _, _, status, _ in records] == [
        (1, "RUNNING"), (1, "PREVIEW"), (2, "RUNNING"), (2, "EXECUTED")]
    assert "tok-secret" not in json.dumps(records, ensure_ascii=False)
    assert gate.count == 2 and summary["sparkx_calls"] == 2


def test_the_write_limit_stops_further_changes(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(), agent)
    gate = agent.WriteGate(confirmed=True, limit=1)
    client = ScriptedDeepSeek([reply(calls=[("create_portfolio", {"name": "A"}), ("create_portfolio", {"name": "B"})]),
                               reply("完成了一项。")])
    converse(settings, client, gate=gate, write=True)
    assert [name for name, _ in fake.calls] == ["create_portfolio"]
    assert tool_replies(client, 1)[1] == agent.WRITE_LIMIT_REACHED


def test_switching_writes_off_stops_a_running_turn(monkeypatch, settings):
    fake = install(monkeypatch, FakeSparkX(), agent)
    write_mode.save(settings, True, "0" * 64)
    records = []

    def switch_off_after_first(*args):
        records.append(args)
        if args[3] == "EXECUTED":  # the owner switches writes off while the turn is running
            write_mode.save(settings, False, write_mode.load(settings)["revision"])

    gate = agent.WriteGate(confirmed=True, record=switch_off_after_first, still_on=lambda: write_mode.enabled(settings))
    client = ScriptedDeepSeek([reply(calls=[("create_portfolio", {"name": "A"})]),
                               reply(calls=[("create_portfolio", {"name": "B"})]), reply("只完成了 A。")])
    converse(settings, client, gate=gate, write=True)
    assert [arguments["name"] for _, arguments in fake.calls] == ["A"]
    assert tool_replies(client, 2)[1] == agent.WRITE_SWITCHED_OFF
    assert [status for _, _, _, status, _ in records] == ["RUNNING", "EXECUTED", "BLOCKED"]


def test_scheduled_lanes_never_see_or_reach_write_tools(monkeypatch, settings):
    write_mode.save(settings, True, "0" * 64)  # even with the switch on
    monkeypatch.setattr(skill_store, "ensure", lambda settings: None)
    rows = [{"profileId": 101, "campaignId": 9002, "campaignName": "SP-Exact", "Spend": 12.5, "Clicks": 19,
             "Conversions": 0}]
    fake = install(monkeypatch, FakeSparkX(rows={"campaign": rows}), agent)
    client = ScriptedDeepSeek([reply(calls=[("batch_update_ads", {"profileId": 101})]),
                               reply("**全公司核心指标**\n| 指标 | 当日 |\n|---|---|\n| 广告花费 | $12.50 |\nJX_STORES: 101")])
    tasks.run_lane(settings, "daily", lane_settings.DEFAULTS["daily"], now=SEND, directory=stores.load(settings),
                   client_factory=lambda s: client)
    assert client.requests[0]["tools"] == READS
    assert "不可用" in tool_replies(client, 1)[0]
    assert all(name not in WRITE_TOOLS for name, _ in fake.calls)


# ---------------------------------------------------------------- the bot's modes

def ask(settings, client, write, record=None):
    return bot_agent.answer_question(settings, "把 SP-Auto 预算改成 50", now=SEND, client_factory=lambda s: client,
                                     write=write, record=record)


def test_a_plan_turn_offers_the_write_tools_and_every_write_skill(monkeypatch, settings):
    install_fake(settings)
    install(monkeypatch, FakeSparkX(), agent)
    client = ScriptedDeepSeek([reply(PLAN)])
    answered = ask(settings, client, "plan", record=lambda *args: None)
    assert answered["write"] == "plan" and answered["writes"] == 0
    first = client.requests[0]
    assert {"batch_update_ads", "create_portfolio"} <= set(first["tools"]) and "list_skills" not in first["tools"]
    system = first["messages"][0]["content"]
    assert "这一轮只做准备" in system and "（含写入 Skill）" in system and "sparkx-edit-ads" in system


def test_read_and_denied_turns_get_no_write_tools(monkeypatch, settings):
    install_fake(settings)
    install(monkeypatch, FakeSparkX(), agent)
    for mode, rule in ((None, "你只能查询"), ("denied", "只对负责人")):
        client = ScriptedDeepSeek([reply("这里只能查询。")])
        answered = ask(settings, client, mode)
        assert answered["write"] == mode
        assert not set(client.requests[0]["tools"]) & WRITE_TOOLS
        system = client.requests[0]["messages"][0]["content"]
        assert rule in system and "sparkx-edit-ads" not in system


def test_write_mode_falls_back_to_read_only_without_write_tools(monkeypatch, settings):
    install_fake(settings)
    install(monkeypatch, FakeSparkX(tools=[item for item in TOOLS if item.name not in WRITE_TOOLS]), agent)
    client = ScriptedDeepSeek([reply("这个 Token 不能修改广告。")])
    answered = ask(settings, client, "execute", record=lambda *args: None)
    assert answered["write"] is None and "你只能查询" in client.requests[0]["messages"][0]["content"]


# ---------------------------------------------------------------- the worker decides the turn

@pytest.fixture
def bot_settings(settings):
    return settings.model_copy(update={"feishu_bot_enabled": True, "feishu_app_id": "cli_synthetic_app",
                                       "feishu_bot_open_id": "ou_bot"})


@pytest.fixture(autouse=True)
def no_reactions(monkeypatch):
    monkeypatch.setattr(bot_service, "acknowledge", lambda settings, meta: "OK")


class Bot:
    """Stands in for bot_agent.answer_question; records the mode the worker chose for each question."""

    def __init__(self, execute=None):
        self.seen, self.execute = [], execute

    def __call__(self, settings, question, *, history, roster, write=None, record=None):
        self.seen.append((question, write))
        if write == "plan":
            return {"answer": PLAN, "provider": "deepseek", "calls": 2, "write": "plan"}
        if write == "execute":
            return self.execute(record)
        return {"answer": "好的。", "provider": "deepseek", "calls": 0, "write": write}


def executed(record):
    request = {"profileId": 102, "request": {"campaigns": [{"campaignId": 9001, "dailyBudget": 50}],
                                             "confirmToken": "***"}}
    record(1, "batch_update_ads", request, "RUNNING", None)
    record(1, "batch_update_ads", request, "EXECUTED", '{"status": "SUCCESS"}')
    return {"answer": "已把 SP-Auto 每日预算改为 $50。", "provider": "deepseek", "calls": 3, "write": "execute",
            "writes": 1}


def process(bot_settings, session_factory, bot):
    return bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=bot)


DM = {"chat": "oc_dm", "kind": "p2p", "mention": False, "sender": LEAD}


def ask_bot(bot_settings, session_factory, bot, text, message, **where):
    with session_factory() as db:
        queue(db, bot_settings, text, message=message, **(where or DM))
    try:
        return process(bot_settings, session_factory, bot)
    finally:
        # Earlier questions move two minutes back, so the intake limit (3 per person a minute) never
        # applies here; plan ages are measured from finished_at, which stays.
        with session_factory() as db:
            for job in db.scalars(select(SyncJob).where(SyncJob.job_type == "feishu_question")):
                job.started_at -= timedelta(minutes=2)
            db.commit()


def question_job(db, message):
    return db.scalar(select(SyncJob).where(SyncJob.job_type == "feishu_question",
                                           SyncJob.metadata_json["message_id"].as_string() == message))


def test_only_a_lead_with_the_switch_on_gets_write_mode(bot_settings, session_factory):
    bot = Bot()
    ask_bot(bot_settings, session_factory, bot, "把预算改成 50", "om_w1")  # the switch is off
    write_mode.save(bot_settings, True, "0" * 64)
    ask_bot(bot_settings, session_factory, bot, "@机器人 把预算改成 50", "om_w2", sender="ou_asker")  # not a lead
    ask_bot(bot_settings, session_factory, bot, "@机器人 把预算改成 50", "om_w3", sender=AD_LEAD)
    assert [mode for _, mode in bot.seen] == [None, "denied", "plan"]
    with session_factory() as db:
        assert question_job(db, "om_w3").metadata_json["requester"] == "广告主管"
        assert question_job(db, "om_w3").metadata_json["write_plan"] is True


def test_a_plan_runs_only_after_the_same_leads_exact_confirmation(bot_settings, session_factory):
    write_mode.save(bot_settings, True, "0" * 64)
    bot = Bot(execute=executed)
    ask_bot(bot_settings, session_factory, bot, "确认执行", "om_c0")  # nothing to confirm yet: a new plan
    ask_bot(bot_settings, session_factory, bot, "把 SP-Auto 预算改成 50", "om_c1")
    ask_bot(bot_settings, session_factory, bot, "确认执行，但是改成 60", "om_c2")  # a changed request: a new plan
    done = ask_bot(bot_settings, session_factory, bot, "确认执行", "om_c3")
    ask_bot(bot_settings, session_factory, bot, "确认执行", "om_c4")  # the plan was used: nothing runs twice
    assert [mode for _, mode in bot.seen] == ["plan", "plan", "plan", "execute", "plan"]
    with session_factory() as db:
        audit = db.scalars(select(SyncJob).where(SyncJob.job_type == "sparkx_write")).all()
        assert [(row.status, row.metadata_json["tool"], row.metadata_json["requester"]) for row in audit] == [
            ("EXECUTED", "batch_update_ads", "老板")]
        assert audit[0].finished_at is not None and audit[0].metadata_json["question_job_id"] == done["job_id"]
        assert audit[0].metadata_json["request"]["request"]["confirmToken"] == "***"
        meta = question_job(db, "om_c3").metadata_json
        assert meta["write_mode"] == "execute" and meta["writes"] == 1 and meta["write_plan"] is False
        reply_row = db.get(NotificationOutbox, done["outbox_ids"][0])
    assert "已把 SP-Auto 每日预算改为 $50" in plain_text(reply_row.payload["card"])


def test_another_person_or_a_late_reply_cannot_confirm_a_plan(bot_settings, session_factory):
    write_mode.save(bot_settings, True, "0" * 64)
    bot = Bot(execute=lambda record: pytest.fail("must not execute"))
    ask_bot(bot_settings, session_factory, bot, "@机器人 把预算改成 50", "om_g1", sender=AD_LEAD)
    ask_bot(bot_settings, session_factory, bot, "@机器人 确认执行", "om_g2", sender=LEAD)  # another lead's plan
    ask_bot(bot_settings, session_factory, bot, "@机器人 确认执行", "om_g3", sender="ou_asker")  # not a lead
    with session_factory() as db:
        plan = question_job(db, "om_g1")
        plan.finished_at -= bot_service.CONFIRM_WINDOW + timedelta(minutes=1)
        db.commit()
    ask_bot(bot_settings, session_factory, bot, "@机器人 确认执行", "om_g4", sender=AD_LEAD)  # too late
    assert [mode for _, mode in bot.seen] == ["plan", "plan", "denied", "plan"]


def test_an_interrupted_execute_turn_is_never_run_again(bot_settings, session_factory):
    write_mode.save(bot_settings, True, "0" * 64)

    def crash(record):
        record(1, "batch_update_ads", {"profileId": 102}, "RUNNING", None)
        raise RuntimeError("the worker stopped mid-change")

    bot = Bot(execute=crash)
    ask_bot(bot_settings, session_factory, bot, "把预算改成 50", "om_i1")
    with pytest.raises(RuntimeError):
        ask_bot(bot_settings, session_factory, bot, "确认执行", "om_i2")
    retried = process(bot_settings, session_factory, Bot(execute=lambda record: pytest.fail("must not run twice")))
    assert retried["status"] == "SUCCEEDED"
    with session_factory() as db:
        text = plain_text(db.get(NotificationOutbox, retried["outbox_ids"][0]).payload["card"])
        audit = db.scalar(select(SyncJob).where(SyncJob.job_type == "sparkx_write"))
        assert audit.status == "RUNNING" and audit.finished_at is None  # the console shows 「已提交，结果未知」
        assert question_job(db, "om_i2").metadata_json["write_plan"] is False
    assert "部分修改可能已经提交" in text


def test_a_failed_execute_turn_says_changes_may_have_been_made(bot_settings, session_factory):
    write_mode.save(bot_settings, True, "0" * 64)

    def failing(record):
        raise SparkXError("SPARKX_TIMEOUT")

    bot = Bot(execute=failing)
    ask_bot(bot_settings, session_factory, bot, "把预算改成 50", "om_f1")
    failed = ask_bot(bot_settings, session_factory, bot, "确认执行", "om_f2")
    with session_factory() as db:
        text = plain_text(db.get(NotificationOutbox, failed["outbox_ids"][0]).payload["card"])
        meta = question_job(db, "om_f2").metadata_json
    assert "暂时查不到广告数据" in text and "部分修改可能已经提交" in text
    assert meta["write_plan"] is False and "部分修改可能已经提交" in meta["answer_text"]
