"""Feishu question intake, private-reader checks and DeepSeek answer rendering."""
import json

import pytest
from sqlalchemy import select

from conftest import AD_LEAD, LEAD
from jx_alert import bot_service
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.notifications.cards import plain_text
from jx_alert.notifications.outbox import NotificationOutbox
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
        assert bot_service.enqueue_group_question(db, bot_settings, event("昨天各店花费", chat="oc_dm", kind="p2p",
                                                                          mention=False, message="om_4"))["status"] == "QUEUED"
        jobs = list(db.scalars(select(SyncJob).where(SyncJob.job_type == "feishu_question")))
        assert [job.metadata_json["question"] for job in jobs] == ["昨天各店花费", "昨天各店花费"]
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


def queue(db, settings, text):
    bot_service.enqueue_group_question(db, settings, event(text))
    db.commit()


def test_answers_are_replied_to_the_original_message(bot_settings, session_factory):
    with session_factory() as db:
        queue(db, bot_settings, "@机器人 昨天各店花费")

    def answer(settings, question):
        assert question == "昨天各店花费"
        return {"answer": "**结论**：昨天花费最高的是香港川页。\n\n| 店铺 | 花费 |\n|---|---|\n| 香港川页 | $3,217.70 |",
                "provider": "deepseek", "calls": 2}

    result = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=answer)
    assert result["status"] == "SUCCEEDED" and len(result["outbox_ids"]) == 1
    with session_factory() as db:
        reply = db.get(NotificationOutbox, result["outbox_ids"][0])
        assert reply.idempotency_key == "group-question:om_1:0"
        assert reply.payload["delivery"]["reply_to_message_id"] == "om_1"
        card = reply.payload["card"]
        assert [element["tag"] for element in card["body"]["elements"]][:2] == ["markdown", "table"]
        assert "香港川页 | $3,217.70" in plain_text(card) and "SparkX MCP 只读查询 2 次" in plain_text(card)


def test_help_unavailable_model_and_unverified_answers(bot_settings, session_factory):
    with session_factory() as db:
        queue(db, bot_settings, "@机器人 你能做什么")
    result = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine,
                                              answer=lambda *a: pytest.fail("help needs no model"))
    with session_factory() as db:
        assert "SparkX" in plain_text(db.get(NotificationOutbox, result["outbox_ids"][0]).payload["card"])

    def unavailable(settings, question):
        raise DeepSeekError("尚未配置DeepSeek凭据。")

    with session_factory() as db:
        bot_service.enqueue_group_question(db, bot_settings, event("@机器人 花费", message="om_2"))
        db.commit()
    result = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=unavailable)
    with session_factory() as db:
        assert "智能问答暂不可用" in plain_text(db.get(NotificationOutbox, result["outbox_ids"][0]).payload["card"])

    def unverified(settings, question):
        return {"answer": None, "provider": "unverified", "calls": 1, "results": [
            {"tool": "query_ads_performance", "result": {"rows": [{"campaignName": "A", "Spend": 12.5}]}}]}

    with session_factory() as db:
        bot_service.enqueue_group_question(db, bot_settings, event("@机器人 花费多少", message="om_3"))
        db.commit()
    result = bot_service.process_one_question(bot_settings, session_factory, session_factory.engine, answer=unverified)
    with session_factory() as db:
        text = plain_text(db.get(NotificationOutbox, result["outbox_ids"][0]).payload["card"])
        assert "未能与 SparkX 结果核对" in text and "A | 12.5" in text


def test_model_markdown_cannot_inject_mentions_or_links():
    cards = bot_service.answer_cards("SparkX 数据助手", "# 标题\n<at id=ou_x></at> 看 [这里](https://evil.example)\n**重点**",
                                     "footer")
    content = "\n".join(e["content"] for e in cards[0]["body"]["elements"] if e["tag"] == "markdown")
    assert "<at" not in content and "](" not in content and "https://" not in content
    assert "**标题**" in content and "**重点**" in content
