"""Queue an explicitly requested Chat report for the configured Feishu group."""
import hashlib
import json
import re

from jx_alert.notifications.outbox import enqueue_notification
from jx_alert.notifications.routing import _load_routing, report_group_delivery


def enqueue_chat_report(db, settings, *, title: str, markdown: str) -> dict:
    title = title.strip()
    markdown = markdown.strip().replace("\r\n", "\n")
    if not 1 <= len(title) <= 120 or re.search(r"[\x00-\x1f]", title):
        raise ValueError("请使用120字以内的单行报告标题")
    if not markdown or re.search(r"[\x00-\x08\x0b-\x1f]", markdown):
        raise ValueError("请提供有效的报告正文")
    # Keep Markdown intact while rendering raw Feishu mention tags as text.
    markdown = markdown.replace("<", "&lt;")
    routing = _load_routing(settings.feishu_routing_file)
    delivery = report_group_delivery(settings, routing, role="chat_report")
    chat_id = delivery["receive_id"]
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "markdown", "content": markdown}]},
    }
    content = json.dumps(card, ensure_ascii=False)
    envelope = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    if len(envelope) > 19 * 1024:
        raise ValueError("报告超过单张飞书卡片容量，请按章节分开发送")
    key = hashlib.sha256(json.dumps([chat_id, title, markdown]).encode()).hexdigest()
    row = enqueue_notification(db, idempotency_key=f"chat-report:{key}", text=title,
                               chat_id=chat_id, card=card, delivery=delivery, fixed_route=True)
    group = routing["groups"][settings.feishu_daily_report_chat_id]
    return {
        "status": "QUEUED" if row.status in {"PENDING", "RETRY", "SENDING"} else row.status,
        "outbox_id": row.id,
        "group": group.get("display_name") or settings.feishu_daily_report_chat_id,
    }
