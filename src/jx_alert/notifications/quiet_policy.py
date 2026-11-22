"""Allow scheduled reports, evidence-backed ads, and authorized replies."""
import re

QUIET_REASON = "飞书精简通知已启用，仅发送定时日报、实质广告异常及机器人原消息回复。"


def quiet_delivery(settings):
    return getattr(settings, "feishu_quiet_delivery_enabled", False)


def quiet_skip():
    return {"status": "SKIPPED", "reason": QUIET_REASON, "outbox_ids": []}


def original_question_reply(key, payload):
    delivery = payload.get('delivery') or {}
    reply_to = delivery.get('reply_to_message_id')
    return (delivery.get('role') in {'group_question', 'private_question'}
            and payload.get('transport') == 'app' and payload.get('card')
            and isinstance(reply_to, str) and bool(reply_to)
            and key.startswith(f'group-question:{reply_to}:'))


def unsolicited_quality_message(key, payload):
    """Never broadcast repair/status pages; authorized questions can ask for them."""
    if original_question_reply(key, payload):
        return False
    if payload.get('morning_message_kind') == 'data_quality':
        return True
    # New focused reports and hourly alerts have source gates before queuing.
    # Their names and archived notice subjects can contain legacy status words.
    if (payload.get('quiet_verified') is True and key.startswith('scheduled-daily:morning:')
            and payload.get('morning_message_kind') in {'health', 'business_daily', 'ad_exception', 'owner_daily'}):
        return False
    if payload.get('hourly_ad_monitor') is True and payload.get('material_ad_alert') is True and key.startswith('app:'):
        return False
    text = json_visible_text(payload.get('card') or {}) + '\n' + str(payload.get('text') or '')
    return bool(re.search(
        r'广告判断待补齐|影响判断的数据缺口|待补齐查询条件|缺少完整基线|缺少.{0,12}成熟归因|'
        r'授权或同步状态异常|授权失效|授权失败|店铺授权异常|目标日报尚未完整采集|'
        r'无法(?:完成)?广告判断|无法判断广告|尚未完整采集|待重新采集校验|'
        r'未取得绩效数据|绩效采集失败|恢复同步后复核|在领星重新授权|'
        r'未知（|未知\(|数据不足|源时区待确认|尚未采集|接口无记录', text))


def json_visible_text(value):
    if isinstance(value, dict):
        return '\n'.join(json_visible_text(item) for item in value.values())
    if isinstance(value, list):
        return '\n'.join(json_visible_text(item) for item in value)
    return value if isinstance(value, str) else ''


def quiet_blocks(settings, key, payload):
    if unsolicited_quality_message(key, payload):
        return True
    if not quiet_delivery(settings):
        return False
    value = payload.get("card") or {}
    if key.startswith("scheduled-daily:"):
        kind = payload.get("morning_message_kind")
        if kind is not None:
            return kind not in {"health", "business_daily", "ad_exception", "owner_daily"}
        # Legacy focused pages have no kind. Preserve today's health and ad pages.
        if key.startswith("scheduled-daily:morning:"):
            title = value.get("header", {}).get("title", {}).get("content", "")
            return not (title.startswith("店铺绩效与违规快报 · ")
                        or title == "商品推广活动待复核")
        return False
    if original_question_reply(key, payload):
        return False
    # The outbox marker is set by the validated server-side producers, not Chat
    # or Feishu inputs. Older generic rows lack sufficient provenance for this policy.
    return not (payload.get("material_ad_alert") is True and key.startswith(
        ("app:", "anomaly:", "chat-analysis:", "chat-ad-kpi:")))
