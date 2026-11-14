"""Isolated Feishu WebSocket intake; persist questions before expensive analysis.

Official contracts:
https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events
https://open.feishu.cn/document/server-docs/im-v1/message/events/receive
https://open.feishu.cn/document/client-docs/bot-v3/obtain-bot-info
"""
import importlib
import json
import logging
import re
import threading

import httpx

from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications.routing import _load_routing, report_group_delivery


logger = logging.getLogger("jx_alert.feishu_bot")
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]+")
_MAX_EVENT_BYTES = 64 * 1024


class BotTransportError(RuntimeError):
    """An intentionally sanitized startup/queue failure."""


class SafeSdkConnectionHandler(logging.Handler):
    """Translate SDK lifecycle records without forwarding their private fields.

    The official SDK logs an authenticated WebSocket URL after a successful
    handshake. A fixed lifecycle message retains that evidence without its URL,
    connection ID, provider error, event payload, or attached traceback.
    """

    _MESSAGES = (
        ("connected to ", "飞书长连接已建立"),
        ("disconnected to ", "飞书长连接已断开"),
        ("trying to reconnect for ", "飞书长连接正在重连"),
        ("connect failed, err: ", "飞书长连接失败"),
    )

    def emit(self, record):
        try:
            message = record.getMessage()
            for prefix, safe_message in self._MESSAGES:
                if message.startswith(prefix):
                    logger.info(safe_message)
                    return
        except Exception:
            # Logging must not affect SDK processing, and handleError(record)
            # would itself print the original, potentially credentialed record.
            return


def _install_safe_sdk_logging():
    sdk_logger = logging.getLogger("Lark")
    # The SDK adds a stdout handler on import. Adding our handler alongside it
    # would still expose URLs. Own its complete output path in this bot process.
    for handler in tuple(sdk_logger.handlers):
        sdk_logger.removeHandler(handler)
    sdk_logger.propagate = False
    sdk_logger.disabled = False
    sdk_logger.setLevel(logging.INFO)
    sdk_logger.addHandler(SafeSdkConnectionHandler())
    websocket_logger = logging.getLogger("websockets.client")
    websocket_logger.disabled = True
    websocket_logger.propagate = False
    for handler in tuple(websocket_logger.handlers):
        websocket_logger.removeHandler(handler)
    websocket_logger.addHandler(logging.NullHandler())


def fetch_bot_open_id(client):
    """Resolve only this authenticated application's active bot identity."""
    if client.base_url != "https://open.feishu.cn":
        raise BotTransportError("Feishu bot discovery requires the official Feishu origin")
    rejected_token = None
    for attempt in range(2):
        token = client._get_token(rejected_token=rejected_token)
        try:
            with httpx.Client(timeout=client.timeout, follow_redirects=False) as http:
                response = http.get(client.base_url + "/open-apis/bot/v3/info",
                    headers={"Authorization": "Bearer " + token})
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise BotTransportError("Feishu bot identity request failed") from None
        if not isinstance(payload, dict):
            raise BotTransportError("Feishu bot identity response is invalid")
        code = payload.get("code")
        if type(code) is not int:
            raise BotTransportError("Feishu bot identity response has no status code")
        if code == client._EXPIRED_TOKEN_CODE and attempt == 0:
            rejected_token = token
            continue
        bot = payload.get("bot")
        if not 200 <= response.status_code < 300 or code != 0 or not isinstance(bot, dict):
            raise BotTransportError("Feishu bot identity request was rejected")
        open_id = bot.get("open_id")
        if (type(bot.get("activate_status")) is not int or bot["activate_status"] != 2
                or not isinstance(open_id, str) or not _OPEN_ID.fullmatch(open_id)):
            raise BotTransportError("Feishu bot is inactive or has no verified identity")
        return open_id
    raise BotTransportError("Feishu bot identity could not be confirmed")


def event_to_dict(data, *, sdk=None):
    """Convert the official SDK's typed event without logging event contents."""
    if not isinstance(data, dict):
        if sdk is None:
            raise ValueError("A typed Feishu event requires its SDK serializer")
        data = json.loads(sdk.JSON.marshal(data))
    if not isinstance(data, dict):
        raise ValueError("Feishu event must be an object")
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > _MAX_EVENT_BYTES:
        raise ValueError("Feishu event exceeds the intake size limit")
    return data


def is_mentioned_question(data, *, app_id, bot_open_id, chat_id, tenant_key=""):
    """Only human text @mentions of this bot inside the configured group pass."""
    if not isinstance(data, dict) or data.get("schema") != "2.0":
        return False
    header, event = data.get("header"), data.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        return False
    if header.get("app_id") != app_id or header.get("event_type") != "im.message.receive_v1":
        return False
    if tenant_key and header.get("tenant_key") != tenant_key:
        return False
    sender, message = event.get("sender"), event.get("message")
    if not isinstance(sender, dict) or sender.get("sender_type") != "user" or not isinstance(message, dict):
        return False
    if (message.get("chat_id") != chat_id or message.get("chat_type") != "group"
            or message.get("message_type") != "text"):
        return False
    if not isinstance(bot_open_id, str) or not _OPEN_ID.fullmatch(bot_open_id):
        return False
    mentions = message.get("mentions")
    if not isinstance(mentions, list) or not any(
            isinstance(item, dict) and isinstance(item.get("id"), dict)
            and item["id"].get("open_id") == bot_open_id for item in mentions):
        return False
    try:
        content = json.loads(message.get("content", ""))
    except (TypeError, ValueError):
        return False
    return (isinstance(content, dict) and isinstance(content.get("text"), str)
            and bool(content["text"].strip()) and len(content["text"]) <= 4000)


def build_event_callback(settings, *, app_id, bot_open_id, chat_id, sdk=None,
                         sessions=None, enqueue=None):
    """The SDK callback only commits the event; the separate worker analyzes it."""
    def receive(data):
        try:
            event = event_to_dict(data, sdk=sdk)
        except (TypeError, ValueError, OverflowError):
            return
        if not is_mentioned_question(event, app_id=app_id, bot_open_id=bot_open_id,
                chat_id=chat_id, tenant_key=getattr(settings, "feishu_bot_tenant_key", "")):
            return
        session_factory, enqueue_question = sessions, enqueue
        if session_factory is None:
            from jx_alert.storage.db import SessionLocal
            session_factory = SessionLocal
        if enqueue_question is None:
            from jx_alert.bot_service import enqueue_group_question
            enqueue_question = enqueue_group_question
        try:
            with session_factory() as db:
                enqueue_question(db, settings, event)
                db.commit()
        except Exception as exc:
            # Raising prevents a database failure being acknowledged as success;
            # the platform can retry and durable message IDs deduplicate it.
            logger.error("Feishu question was not queued (%s)", type(exc).__name__)
            raise BotTransportError("Feishu question persistence failed") from None
    return receive


def _load_sdk():
    try:
        return importlib.import_module("lark_oapi")
    except ImportError:
        raise BotTransportError("Install the optional Feishu bot dependencies") from None


def run_bot(settings):
    """Run in its own process; neither MCP routes nor MCP OAuth state are changed."""
    if not getattr(settings, "feishu_bot_enabled", False):
        logger.info("Feishu group question service is disabled")
        return
    if (settings.app_dry_run or not settings.feishu_enabled
            or settings.feishu_transport != "app"):
        raise BotTransportError("Feishu group questions require enabled application delivery")
    route = report_group_delivery(settings, _load_routing(settings.feishu_routing_file), role="group_questions")
    credentials = FeishuAppClient.from_settings(settings)
    identity = fetch_bot_open_id(credentials)
    configured_id = getattr(settings, "feishu_bot_open_id", "")
    if configured_id and configured_id != identity:
        raise BotTransportError("Configured bot identity does not match the application")
    # Process-only discovery is also available to the queue's independent checks.
    settings.feishu_bot_open_id = identity
    settings.feishu_app_id = credentials._app_id
    sdk = _load_sdk()
    _install_safe_sdk_logging()
    callback = build_event_callback(settings, app_id=credentials._app_id,
        bot_open_id=identity, chat_id=route["receive_id"], sdk=sdk)
    handler = sdk.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(callback).build()
    client = sdk.ws.Client(credentials._app_id, credentials._app_secret,
        event_handler=handler, log_level=sdk.LogLevel.INFO)
    from jx_alert.bot_service import run_question_worker
    threading.Thread(target=run_question_worker, name="feishu-question-worker", daemon=True).start()
    logger.info("Feishu group question intake is starting")
    client.start()


def main():
    from jx_alert.config import get_settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        run_bot(get_settings())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Provider exceptions may contain credentials or authenticated URLs.
        logger.error("Feishu question service stopped (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
