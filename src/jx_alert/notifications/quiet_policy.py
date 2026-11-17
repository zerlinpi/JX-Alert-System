"""Allow scheduled reports, evidence-backed ads, and authorized replies."""

QUIET_REASON = "飞书精简通知已启用，仅发送定时日报、实质广告异常及机器人原消息回复。"


def quiet_delivery(settings):
    return getattr(settings, "feishu_quiet_delivery_enabled", False)


def quiet_skip():
    return {"status": "SKIPPED", "reason": QUIET_REASON, "outbox_ids": []}


def quiet_blocks(settings, key, payload):
    if not quiet_delivery(settings):
        return False
    value = payload.get("card") or {}
    delivery = payload.get("delivery") or {}
    role = delivery.get("role")
    if key.startswith("scheduled-daily:"):
        kind = payload.get("morning_message_kind")
        if kind is not None:
            return kind not in {"health", "business_daily", "ad_exception"}
        # Legacy focused pages have no kind. Preserve today's health and ad pages.
        if key.startswith("scheduled-daily:morning:"):
            title = value.get("header", {}).get("title", {}).get("content", "")
            return not (title.startswith("店铺绩效与违规快报 · ")
                        or title == "商品推广活动待复核")
        return False
    reply_to = delivery.get("reply_to_message_id")
    if (role in {"group_question", "private_question"} and payload.get("transport") == "app"
            and isinstance(reply_to, str) and reply_to and value
            and key.startswith(f"group-question:{reply_to}:")):
        return False
    # The outbox marker is set by the validated server-side producers, not Chat
    # or Feishu inputs. Older generic rows lack sufficient provenance for this policy.
    return not (payload.get("material_ad_alert") is True and key.startswith(
        ("app:", "anomaly:", "chat-analysis:", "chat-ad-kpi:")))
