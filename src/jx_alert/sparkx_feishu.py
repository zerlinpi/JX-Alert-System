"""Operator-only manual bridge. A normal SparkX MCP read never queues a message."""
import argparse
import hashlib
import json
import re
import sys
from datetime import timedelta

import anyio

from jx_alert.notifications.sparkx_card import render_page
from jx_alert.sparkx_direct import (
    SparkXDirectError, call_sparkx, check_authorized_profiles, parse_profile_ids,
    page_size_for, validate_performance_query,
)

PREFIX = "sparkx-page:v1:"
ROLE = "sparkx_performance_page"
CONTENT_CONTRACT = "scoped-structured-page-v1"


def _route(db, settings, profile):
    from jx_alert.notifications.routing import _load_routing, report_group_delivery
    from jx_alert.storage.models import Store
    from jx_alert.store_controls import notifications_enabled

    if (not settings.sparkx_feishu_enabled or settings.app_dry_run or not settings.feishu_enabled
            or settings.feishu_transport != "app" or settings.feishu_quiet_delivery_enabled
            or profile not in parse_profile_ids(settings.sparkx_profile_ids)):
        raise SparkXDirectError("SparkX 飞书手动投递未启用，或当前通知策略不允许")
    try:
        routing = _load_routing(settings.feishu_routing_file)
        route = routing["sparkx_profile_routes"][str(profile)]
        dest = report_group_delivery(settings, routing, role=ROLE)
        sid, generation, currency = route["store_id"], route["generation"], route["currency"]
        if not (isinstance(sid, str) and re.fullmatch("[1-9][0-9]{0,99}", sid)
                and re.fullmatch("[0-9a-f]{32}", generation) and re.fullmatch("[A-Z]{3}", currency)
                and route["chat_id"] == dest["receive_id"]
                and routing["stores"][sid]["chat_id"] == dest["routing_alias"]
                and routing["groups"][dest["routing_alias"]]["verified_member_open_ids"]
                and notifications_enabled(settings, sid)):
            raise ValueError
        store = db.get(Store, sid)
        if store is None or store.status != 1 or store.currency_code != currency:
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        raise SparkXDirectError("未核实 SparkX 账户、启用的店铺、币种与指定飞书群的对应关系") from None
    return {**dest, "sparkx_profile_id": str(profile), "sparkx_generation": generation,
            "sparkx_currency": currency}, sid


def page_fresh(created_at, now):
    return timedelta(0) <= now - created_at <= timedelta(minutes=30)


def delivery_blocked(db, settings, key, payload, created_at, now):
    """Revalidate the frozen scope at dispatch, including revocation or generation change."""
    from jx_alert.notifications.store_policy import paused_delivery

    try:
        dest = payload["delivery"]
        profile, = parse_profile_ids(dest["sparkx_profile_id"])
        expected, sid = _route(db, settings, profile)
        return (not re.fullmatch(re.escape(PREFIX) + "[0-9a-f]{64}", key)
                or payload.get("transport") != "app" or payload.get("store_id") != sid
                or payload.get("open_ids") != [] or not payload.get("card")
                or not payload.get("fixed_route") or dest != {**expected, "sparkx_content_contract": CONTENT_CONTRACT}
                or not page_fresh(created_at, now)
                or paused_delivery(settings, payload, created_at))
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return True


async def prepare(settings, query, *, enqueue=False, session_factory=None):
    """Only a fixed upstream call can produce cards for the durable outbox."""
    if not isinstance(query, dict):
        raise SparkXDirectError("需要单个 SparkX 查询对象")
    allowed = parse_profile_ids(settings.sparkx_profile_ids)
    validate_performance_query(query, allowed)
    profile, = query["profileIds"]
    if enqueue:
        if session_factory is None:
            from jx_alert.storage.db import SessionLocal
            session_factory = SessionLocal
        with session_factory() as db:
            frozen = _route(db, settings, profile)
    token = settings.sparkx_ai_token.get_secret_value()
    context = await call_sparkx("get_user_authorized_context", {}, token=token)
    check_authorized_profiles(context, frozenset({profile}))
    result = await call_sparkx("get_ads_perf", query, token=token)
    page = render_page(result, query)
    if not enqueue:
        preview = {"status": "PREVIEW", "eligible_result": page.eligible, "card": page.card}
        if page.delivery_card is not None and not page.eligible:
            preview["delivery_preview"] = page.delivery_card
        return preview
    from jx_alert.notifications.outbox import enqueue_notification
    from jx_alert.notifications.quiet_policy import quiet_blocks

    with session_factory.begin() as db:
        dest, sid = _route(db, settings, profile)
        if frozen != (dest, sid) or page.delivery_card is None or page.currency != dest["sparkx_currency"]:
            raise SparkXDirectError("结果或接收范围未核实，仅可预览；未入队")
        # Retries reuse the same logical page. A new upstream requestId,
        # differently ordered keys, or a new wording of userContext is not a new report.
        identity = {**query, "page": query.get("page", 1), "pageSize": page_size_for(query)}
        del identity["userContext"]
        material = json.dumps([identity, dest, sid], ensure_ascii=False, sort_keys=True)
        key = PREFIX + hashlib.sha256(material.encode()).hexdigest()
        if quiet_blocks(settings, key, {"card": page.delivery_card}):
            raise SparkXDirectError("当前通知策略禁止投递此页")
        row = enqueue_notification(db, idempotency_key=key, chat_id=dest["receive_id"],
                                   store_id=sid, delivery={**dest, "sparkx_content_contract": CONTENT_CONTRACT},
                                   card=page.delivery_card, fixed_route=True,
                                   text="SparkX 广告表现（单页原生结果）")
        return {"status": row.status, "outbox_id": row.id}


def main():
    from jx_alert.config import get_settings

    parser = argparse.ArgumentParser(description="预览一个 SparkX 查询页；不自动翻页或发送。")
    parser.add_argument("--enqueue", action="store_true", help="操作员明确要求把此页加入已核验的群队列")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(32_769)
        if len(raw) > 32_768:
            raise ValueError
        query = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise SystemExit("请通过标准输入提供有效的 SparkX 查询 JSON（最多 32 KiB）") from None

    async def run():
        return await prepare(get_settings(), query, enqueue=args.enqueue)

    try:
        print(json.dumps(anyio.run(run), ensure_ascii=False))
    except SparkXDirectError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        # Do not print SQL parameters (cards), private settings, or raw upstream bodies.
        raise SystemExit("无法完成 SparkX 飞书操作，请核对本地配置和数据库状态") from None


if __name__ == "__main__":
    main()
