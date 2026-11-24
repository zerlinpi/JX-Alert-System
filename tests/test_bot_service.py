"""Feishu message intake, private-reader checks, conversation memory and answer rendering."""
import json

import pytest
from sqlalchemy import select

from conftest import AD_LEAD, LEAD
from jx_alert import bot_service
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.notifications.cards import plain_text
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.sparkx.client import SparkXError
from jx_alert.storage.models import SyncJob


@pytest.fixture
def bot_settings(settings):
    return settings.model_copy(update={"feishu_bot_enabled": True, "feishu_app_id": "cli_synthetic_app",
                                       "feishu_bot_open_id": "ou_bot"})


def event(text="@机器人 昨天各店花费", *, chat="oc_reports", kind="group", sender="ou_asker", message="om_1",
          mention=True):
    mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}] if mention else []
    return {"schema": "2.0", "header": {"event_type": "im.message.receive_v1", "app_id": "cli_synthetic_app"},
            "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": sender}},
                      "message": {"chat_id": chat, "chat_type": kind, "message_type": "text", "message_id": message,
                                  "mentions": mentions, "content": json.dumps({"text": text.replace("@机器人", "@_user_1")})}}}


def test_group_mentions_in_the_report_group_are_queued_once(bot_settings, session_factory):
    with session_factory() as db:
        assert bot_service.enqueue_group_question(db, bot_settings, event())["status"] == "QUEUED"
        assert bot_service.enqueue_group_question(db, bot_settings, event())["status"] == "REUSED"
        assert bot_service.enqueue_group_question(db, bot_settings, event(chat="oc_other", message="om_2"))["status"] == "IGNORED"
        assert bot_service.enqueue_group_question(db, bot_settings, event(mention=False, message="om_3"))["status"] == "IGNORED"
        assert bot_service.enqueue_group_question(db, bot_settings, event("你好", chat="oc_dm", kind="p2p",
                                                                          mention=False, message="om_4"))["status"] == "QUEUED"
        jobs = list(db.scalars(select(SyncJob).where(SyncJob.job_type == "feishu_question")))
        assert [job.metadata_json["question"] for job in jobs] == ["昨天各店花费", "你好"]
        assert jobs[0].metadata_json["question_route"]["receive_id"] == "oc_reports"
    disabled = bot_settings.model_copy(update={"feishu_bot_enabled": False})
    with session_factory() as db:
        assert bot_service.enqueue_group_question(db, disabled, event(message="om_9"))["status"] == "DISABLED"


def test_private_readers_are_leads_operators_or_report_group_members(bot_settings, monkeypatch):
    delivery = {"role": "private_question", "receive_id_type": "chat_id", "receive_id": "oc_dm",
                "sender_open_id": LEAD}
    assert bot_service.validate_question_delivery(bot_settings, delivery)
    assert bot_service.validate_question_delivery(bot_settings, {**delivery, "sender_open_id": AD_LEAD})
    import jx_alert.admin_feishu as admin_feishu
    monkeypatch.setattr(admin_feishu, "members", lambda settings, chat: {"ou_member": "成员"})
    assert bot_service.validate_question_delivery(bot_settings, {**delivery, "sender_open_id": "ou_member"})
    assert not bot_service.validate_question_delivery(bot_settings, {**delivery, "sender_open_id": "ou_stranger"})
    assert not bot_service.validate_question_delivery(bot_settings, {**delivery, "receive_id": "oc_reports"})


def queue(db, settings, text, **kwargs):
    bot_service.enqueue_group_question(db, settings, event(text, **kwargs))
    db.commit()


def reply_card(session_factory, result):
    with session_factory() as db:
        return db.get(NotificationOutbox, result["outbox_ids"][0])


def test_data_answers_are_replied_with_tables_and_a_source_line(bot_settings, session_factory):
    with session_factory() as db:
        queue(db, bot_settings, "@机器人 昨天各店花费")

    def answer(settings, question, *, history, roster):
        assert question == "昨天各店花费" and history == []
        return {"answer": "**结论**：昨天花费最高的是香港川页。\n\n| 店铺 | 花费 |\n|---|---|\n| 香港川页 | $3,217.70 |",
                "provider": "deepseek", "calls": 2}

    result = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=answer)
    assert result["status"] == "SUCCEEDED" and len(result["outbox_ids"]) == 1
    reply = reply_card(session_factory, result)
    assert reply.idempotency_key == "group-question:om_1:0"
    assert reply.payload["delivery"]["reply_to_message_id"] == "om_1"
    card = reply.payload["card"]
    assert [element["tag"] for element in card["body"]["elements"]][:2] == ["markdown", "table"]
    text = plain_text(card)
    assert "香港川页 | $3,217.70" in text and "SparkX MCP 只读查询 2 次" in text


def test_free_conversation_remembers_the_same_persons_recent_turns(bot_settings, session_factory):
    seen = []

    def answer(settings, question, *, history, roster):
        seen.append([turn["question"] for turn in history])
        return {"answer": f"回答：{question}", "provider": "deepseek", "calls": 0}

    with session_factory() as db:
        queue(db, bot_settings, "@机器人 你好", message="om_a")
    first = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=answer)
    with session_factory() as db:
        queue(db, bot_settings, "@机器人 那上周呢", message="om_b")
        queue(db, bot_settings, "@机器人 我是别人", message="om_c", sender="ou_other")
    bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=answer)
    bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=answer)
    assert seen == [[], ["你好"], []]  # another person's turns in the group are not shared
    text = plain_text(reply_card(session_factory, first).payload["card"])
    assert "回答：你好" in text and "SparkX" not in text  # no source line when SparkX was not queried


@pytest.mark.parametrize("error,expected", [(DeepSeekError("DeepSeek繁忙，请稍后重试。"), "DeepSeek 暂时无法回答"),
                                            (SparkXError("SPARKX_AUTH_REJECTED"), "暂时无法查询 SparkX")])
def test_failures_reply_with_a_plain_reason(bot_settings, session_factory, error, expected):
    with session_factory() as db:
        queue(db, bot_settings, "@机器人 花费")

    def failing(settings, question, **kwargs):
        raise error

    result = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=failing)
    assert expected in plain_text(reply_card(session_factory, result).payload["card"])


def test_model_markdown_cannot_inject_mentions_or_links():
    cards = bot_service.answer_cards("JX 广告助手", "# 标题\n<at id=ou_x></at> 看 [这里](https://evil.example)\n**重点**",
                                     "footer")
    content = "\n".join(e["content"] for e in cards[0]["body"]["elements"] if e["tag"] == "markdown")
    assert "<at" not in content and "](" not in content and "https://" not in content
    assert "**标题**" in content and "**重点**" in content
