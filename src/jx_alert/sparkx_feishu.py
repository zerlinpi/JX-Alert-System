"""Operator-only manual bridge. A normal SparkX MCP read never queues a message."""
import argparse
import json
import re
import sys
from datetime import timedelta
from hashlib import sha256

import anyio

from jx_alert.notifications.sparkx_card import render_page
from jx_alert.sparkx_direct import (
    SparkXDirectError, call_sparkx, check_authorized_profiles, parse_profile_ids,
    validate_performance_query,
)

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
    from jx_alert.notifications.sparkx_export import valid_export_key
    from jx_alert.notifications.store_policy import paused_delivery

    try:
        dest = payload["delivery"]
        profile, = parse_profile_ids(dest["sparkx_profile_id"])
        expected, sid = _route(db, settings, profile)
        return (not valid_export_key(key, payload)
                or payload.get("transport") != "app" or payload.get("store_id") != sid
                or payload.get("open_ids") != [] or not payload.get("card")
                or not payload.get("fixed_route") or dest != {**expected, "sparkx_content_contract": CONTENT_CONTRACT}
                or not page_fresh(created_at, now)
                or paused_delivery(settings, payload, created_at))
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return True


async def prepare(settings, query, *, enqueue=False, inspect=False, fresh_after=None,
                  ack_delivered=False, session_factory=None):
    """Only a fixed upstream call can produce cards for the durable outbox."""
    from jx_alert.notifications.sparkx_export import ExportHistory

    if ((inspect and enqueue) or fresh_after is not None and (
            not enqueue or type(fresh_after) is not int or not 0 < fresh_after < 2**63)
            or ack_delivered and fresh_after is None):
        raise SparkXDirectError("仅 --enqueue 可用 --fresh-after 有效编号；确认送达仅用于显式新导出")
    if not isinstance(query, dict):
        raise SparkXDirectError("需要单个 SparkX 查询对象")
    allowed = parse_profile_ids(settings.sparkx_profile_ids)
    validate_performance_query(query, allowed)
    profile, = query["profileIds"]
    if enqueue or inspect:
        if session_factory is None:
            from jx_alert.storage.db import SessionLocal
            session_factory = SessionLocal
        with session_factory() as db:
            frozen = _route(db, settings, profile)
            if inspect:
                return ExportHistory(db, query, *frozen, CONTENT_CONTRACT).view()
            engine = db.get_bind()
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
    from jx_alert.notifications.quiet_policy import quiet_blocks
    from jx_alert.storage.job_lock import JobAlreadyRunning, database_job_lock

    # Serialize publishers for the same actual recipient/profile across routing
    # generations. No vendor I/O, HTTP send or FEISHU token request under this lock.
    lock_name = "sparkx_" + sha256(json.dumps(
        [frozen[0]["sparkx_profile_id"], frozen[0]["receive_id"]]).encode()).hexdigest()[:32]
    try:
        with database_job_lock(engine, lock_name, pin_connection=True) as connection:
            options = {"bind": connection} if connection is not None else {}
            with session_factory(**options) as db, db.begin():
                dest, sid = _route(db, settings, profile)
                if frozen != (dest, sid) or page.delivery_card is None or page.currency != dest["sparkx_currency"]:
                    raise SparkXDirectError("结果或接收范围未核实，仅可预览；未入队")
                if quiet_blocks(settings, "sparkx-page:", {"card": page.delivery_card}):
                    raise SparkXDirectError("当前通知策略禁止投递此页")
                row = ExportHistory(db, query, dest, sid, CONTENT_CONTRACT).create_or_reuse(
                    dest, sid, page.delivery_card, fresh_after=fresh_after, ack_delivered=ack_delivered)
                return {"status": row.status, "outbox_id": row.id}
    except JobAlreadyRunning:
        raise SparkXDirectError("同一 SparkX 账户与群的导出正在处理；稍后用同一请求重试") from None


def main():
    from jx_alert.config import get_settings

    parser = argparse.ArgumentParser(description="预览一个 SparkX 查询页；不自动翻页或发送。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enqueue", action="store_true", help="操作员明确要求把此页加入已核验的群队列")
    mode.add_argument("--inspect", action="store_true", help="仅核对本地导出状态，不调用 SparkX 或飞书")
    parser.add_argument("--fresh-after", type=int, metavar="OUTBOX_ID", help="显式请求同一查询页的新导出")
    parser.add_argument("--ack-delivered", action="store_true", help="确认前一版已送达，而不是重试旧投递")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(32_769)
        if len(raw) > 32_768:
            raise ValueError
        query = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise SystemExit("请通过标准输入提供有效的 SparkX 查询 JSON（最多 32 KiB）") from None

    async def run():
        return await prepare(get_settings(), query, enqueue=args.enqueue, inspect=args.inspect,
                             fresh_after=args.fresh_after, ack_delivered=args.ack_delivered)

    try:
        print(json.dumps(anyio.run(run), ensure_ascii=False))
    except SparkXDirectError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        # Do not print SQL parameters (cards), private settings, or raw upstream bodies.
        raise SystemExit("无法完成 SparkX 飞书操作，请核对本地配置和数据库状态") from None


if __name__ == "__main__":
    main()
