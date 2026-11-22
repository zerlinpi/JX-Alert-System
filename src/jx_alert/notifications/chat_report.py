"""Queue an explicitly requested Chat report for the configured Feishu group."""
import hashlib
import json
import re
from pathlib import Path

from jx_alert.notifications.outbox import IN_FLIGHT, enqueue_notification
from jx_alert.notifications.quiet_policy import quiet_delivery, quiet_skip
from jx_alert.notifications.routing import _load_routing, report_group_delivery
from jx_alert.store_controls import notification_states


def enqueue_chat_report(db, settings, *, title: str, markdown: str, store_ids: list[str] | None = None) -> dict:
    if quiet_delivery(settings):
        return quiet_skip()
    if store_ids is not None:
        raw_scope = json.loads(Path(settings.collection_scope_file).read_text(encoding="utf-8"))
        scope = {str(int(sid)) for sid in raw_scope["store_ids"]}
        if not store_ids or len(store_ids) > len(scope) or any(sid not in scope for sid in store_ids):
            raise ValueError("请提供报告覆盖的有效店铺编号")
        store_ids = sorted(set(store_ids))
    if any(not state[0] for state in notification_states(settings, store_ids).values()):
        message = ("报告包含已关闭通知的店铺，请移除这些店铺及其数据"
                   if store_ids is not None else "有店铺已关闭通知，请注明报告覆盖的店铺编号")
        return {"status": "SKIPPED", "reason": message, "outbox_ids": []}
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
    identity = [chat_id, title, markdown]
    if store_ids is not None:
        identity.append(store_ids)
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    row = enqueue_notification(db, idempotency_key=f"chat-report:{key}", text=title,
                               chat_id=chat_id, card=card, delivery=delivery, fixed_route=True, store_ids=store_ids)
    group = routing["groups"][settings.feishu_daily_report_chat_id]
    return {
        "status": "QUEUED" if row.status in IN_FLIGHT else row.status,
        "outbox_id": row.id,
        "group": group.get("display_name") or settings.feishu_daily_report_chat_id,
    }
