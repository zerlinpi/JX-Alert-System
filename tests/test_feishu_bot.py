from copy import deepcopy
import json
import io
import logging
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from jx_alert import feishu_bot as bot
from jx_alert.notifications.feishu_app import FeishuAppClient


@pytest.fixture
def sdk_logging():
    names = ("Lark", "websockets.client")
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level,
                    logging.getLogger(name).disabled, logging.getLogger(name).propagate)
             for name in names}
    try:
        yield
    finally:
        for name, (handlers, level, disabled, propagate) in saved.items():
            target = logging.getLogger(name)
            target.handlers = handlers
            target.setLevel(level)
            target.disabled, target.propagate = disabled, propagate


def incoming():
    return {"schema": "2.0", "header": {"event_type": "im.message.receive_v1",
        "app_id": "cli_bot", "tenant_key": "tenant", "event_id": "event-1"},
        "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_member"}},
            "message": {"message_id": "om_question", "chat_id": "oc_reports", "chat_type": "group",
                "message_type": "text", "content": json.dumps({"text": "@_user_1 分析5174店铺"}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_this_bot"}}]}}}


def accepted(data):
    return bot.is_mentioned_question(data, app_id="cli_bot", bot_open_id="ou_this_bot", chat_id="oc_reports")


@pytest.mark.parametrize("alter", [
    lambda v: v.update(schema="1.0"),
    lambda v: v["header"].update(app_id="cli_other"),
    lambda v: v["header"].update(event_type="im.message.read_v1"),
    lambda v: v["event"]["sender"].update(sender_type="app"),
    lambda v: v["event"]["message"].update(chat_id="oc_other"),
    lambda v: v["event"]["message"].update(chat_type="p2p"),
    lambda v: v["event"]["message"].update(message_type="image"),
    lambda v: v["event"]["message"].update(mentions=[]),
    lambda v: v["event"]["message"].update(mentions=[{"id": {"open_id": "ou_someone_else"}}]),
    lambda v: v["event"]["message"].update(mentions=[{"id": "ou_this_bot"}]),
    lambda v: v["event"]["message"].update(content="not json"),
    lambda v: v["event"]["message"].update(content="[]"),
    lambda v: v["event"]["message"].update(content='{"text":" "}'),
    lambda v: v["event"]["message"].update(content=json.dumps({"text": "x" * 4001})),
])
def test_intake_ignores_other_apps_bots_groups_and_nonquestions(alter):
    data = incoming()
    assert accepted(data)
    alter(data)
    assert not accepted(data)


def test_optional_tenant_match_and_verified_self_mention():
    data = incoming()
    kwargs = {"app_id": "cli_bot", "bot_open_id": "ou_this_bot", "chat_id": "oc_reports"}
    assert bot.is_mentioned_question(data, **kwargs, tenant_key="tenant")
    assert not bot.is_mentioned_question(data, **kwargs, tenant_key="other-tenant")
    assert not bot.is_mentioned_question(data, **{**kwargs, "bot_open_id": ""})


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def test_bot_identity_uses_this_apps_tenant_token_and_no_redirects(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200})
        assert request.method == "GET" and request.url.path == "/open-apis/bot/v3/info"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"code": 0, "bot": {"activate_status": 2, "open_id": "ou_this_bot"}})
    mock_http(monkeypatch, handler)
    assert bot.fetch_bot_open_id(FeishuAppClient("cli_bot", "test-secret")) == "ou_this_bot"
    assert len(seen) == 2


def test_identity_refreshes_expired_token_once(monkeypatch):
    states = {"tokens": 0, "lookups": 0}
    def handler(request):
        if request.url.path.endswith("/internal"):
            states["tokens"] += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": str(states["tokens"]), "expire": 7200})
        states["lookups"] += 1
        if states["lookups"] == 1:
            return httpx.Response(401, json={"code": 99991663})
        return httpx.Response(200, json={"code": 0, "bot": {"activate_status": 2, "open_id": "ou_this_bot"}})
    mock_http(monkeypatch, handler)
    assert bot.fetch_bot_open_id(FeishuAppClient("cli_bot", "test-secret")) == "ou_this_bot"
    assert states == {"tokens": 2, "lookups": 2}


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://other.invalid/private-token"}),
    httpx.Response(400, json={"code": 123, "msg": "private-token"}),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"code": True, "bot": {"open_id": "ou_wrong"}}),
    httpx.Response(200, json={"code": 0, "bot": {"activate_status": 1, "open_id": "ou_disabled"}}),
    httpx.Response(200, json={"code": 0, "bot": {"activate_status": 2, "open_id": "not-an-open-id"}}),
])
def test_unverified_identity_fails_without_response_or_token_leak(monkeypatch, response):
    client = FeishuAppClient("cli_bot", "private-secret")
    monkeypatch.setattr(client, "_get_token", lambda **kw: "private-token")
    mock_http(monkeypatch, lambda request: response)
    with pytest.raises(bot.BotTransportError) as exc:
        bot.fetch_bot_open_id(client)
    assert "private" not in str(exc.value)


def test_event_conversion_and_size_limit_do_not_require_installed_sdk():
    data = incoming()
    assert bot.event_to_dict(data) == data
    sdk = SimpleNamespace(JSON=SimpleNamespace(marshal=lambda value: json.dumps(data)))
    assert bot.event_to_dict(object(), sdk=sdk) == data
    with pytest.raises(ValueError):
        bot.event_to_dict(object())
    with pytest.raises(ValueError, match="size"):
        bot.event_to_dict({"extra": "x" * 65536})


class Session:
    def __init__(self):
        self.commits = 0
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def commit(self):
        self.commits += 1


def test_sdk_callback_only_commits_queue_and_ignores_unrelated_messages():
    calls = []
    session = Session()
    settings = SimpleNamespace(feishu_bot_tenant_key="tenant")
    callback = bot.build_event_callback(settings, app_id="cli_bot", bot_open_id="ou_this_bot",
        chat_id="oc_reports", sessions=lambda: session, enqueue=lambda db, s, event: calls.append(deepcopy(event)))
    callback(incoming())
    assert len(calls) == 1 and session.commits == 1
    wrong = incoming()
    wrong["event"]["message"]["chat_id"] = "oc_other"
    callback(wrong)
    assert len(calls) == 1 and session.commits == 1


def test_queue_error_is_not_acknowledged_and_does_not_leak_payload(caplog):
    def fail(*args):
        raise RuntimeError("private-secret and customer message")
    session = Session()
    callback = bot.build_event_callback(SimpleNamespace(), app_id="cli_bot", bot_open_id="ou_this_bot",
        chat_id="oc_reports", sessions=lambda: session, enqueue=fail)
    with pytest.raises(bot.BotTransportError) as exc:
        callback(incoming())
    assert session.commits == 0
    assert "private-secret" not in caplog.text + str(exc.value)
    assert "customer message" not in caplog.text + str(exc.value)


def test_disabled_bot_does_not_import_sdk_read_credentials_or_touch_mcp(monkeypatch):
    def forbidden(*args):
        pytest.fail("Disabled service must have no external effects")
    monkeypatch.setattr(bot, "_load_sdk", forbidden)
    monkeypatch.setattr(bot.FeishuAppClient, "from_settings", forbidden)
    bot.run_bot(SimpleNamespace(feishu_bot_enabled=False))


def test_dry_run_bot_refuses_connection_before_reading_credentials(monkeypatch):
    settings = SimpleNamespace(feishu_bot_enabled=True, app_dry_run=True, feishu_enabled=True, feishu_transport="app")
    monkeypatch.setattr(bot.FeishuAppClient, "from_settings", lambda s: pytest.fail("No credentials needed"))
    with pytest.raises(bot.BotTransportError):
        bot.run_bot(settings)


def test_long_connection_uses_official_dispatcher_and_separate_worker(monkeypatch, sdk_logging):
    state = {}
    class Builder:
        def register_p2_im_message_receive_v1(self, callback):
            state["callback"] = callback
            return self
        def build(self):
            return "handler"
    class Dispatcher:
        @staticmethod
        def builder(encrypt, verification):
            assert (encrypt, verification) == ("", "")
            return Builder()
    class Ws:
        def __init__(self, app_id, secret, **kwargs):
            assert app_id == "cli_bot" and secret == "test-secret"
            assert kwargs == {"event_handler": "handler", "log_level": "info"}
        def start(self):
            state["started"] = True
    class Thread:
        def __init__(self, **kwargs):
            state["thread"] = kwargs
        def start(self):
            state["worker_started"] = True
    settings = SimpleNamespace(feishu_bot_enabled=True, app_dry_run=False, feishu_enabled=True,
        feishu_transport="app", feishu_routing_file="not-read", feishu_bot_open_id="")
    module = ModuleType("jx_alert.bot_service")
    module.run_question_worker = lambda: None
    monkeypatch.setitem(sys.modules, "jx_alert.bot_service", module)
    monkeypatch.setattr(bot, "_load_routing", lambda path: {})
    monkeypatch.setattr(bot, "report_group_delivery", lambda *a, **kw: {"receive_id": "oc_reports"})
    monkeypatch.setattr(bot.FeishuAppClient, "from_settings", lambda settings: SimpleNamespace(_app_id="cli_bot", _app_secret="test-secret"))
    monkeypatch.setattr(bot, "fetch_bot_open_id", lambda client: "ou_this_bot")
    monkeypatch.setattr(bot, "_load_sdk", lambda: SimpleNamespace(EventDispatcherHandler=Dispatcher,
        LogLevel=SimpleNamespace(INFO="info"), ws=SimpleNamespace(Client=Ws)))
    monkeypatch.setattr(bot.threading, "Thread", Thread)
    bot.run_bot(settings)
    assert settings.feishu_bot_open_id == "ou_this_bot"
    assert settings.feishu_app_id == "cli_bot"
    assert state["started"] and state["worker_started"]
    assert state["thread"]["target"] is module.run_question_worker
    assert state["thread"]["daemon"] is True
    sdk_logger = logging.getLogger("Lark")
    assert not sdk_logger.disabled and not sdk_logger.propagate
    assert sdk_logger.level == logging.INFO
    assert len(sdk_logger.handlers) == 1
    assert isinstance(sdk_logger.handlers[0], bot.SafeSdkConnectionHandler)
    assert "jx_alert.mcp_server" not in bot.__dict__


@pytest.mark.parametrize("sdk_message,safe_message", [
    ("connected to wss://private.invalid?secret=private-url [conn_id=private-id]", "飞书长连接已建立"),
    ("disconnected to wss://private.invalid?secret=private-url [conn_id=private-id]", "飞书长连接已断开"),
    ("trying to reconnect for the 1st time [conn_id=private-id]", "飞书长连接正在重连"),
    ("connect failed, err: secret=private-error [conn_id=private-id]", "飞书长连接失败"),
])
def test_sdk_lifecycle_is_visible_without_forwarding_any_original_record(
    sdk_logging, sdk_message, safe_message, caplog, capsys,
):
    original_output = io.StringIO()
    sdk_logger = logging.getLogger("Lark")
    sdk_logger.handlers = [logging.StreamHandler(original_output), logging.StreamHandler(original_output)]
    sdk_logger.propagate = True
    sdk_logger.disabled = True
    bot._install_safe_sdk_logging()
    with caplog.at_level(logging.INFO, logger=bot.logger.name):
        sdk_logger.info(sdk_message)
    assert [record.getMessage() for record in caplog.records] == [safe_message]
    output = capsys.readouterr()
    combined = original_output.getvalue() + output.out + output.err + caplog.text
    assert original_output.getvalue() == ""
    assert "private" not in combined and "wss://" not in combined
    assert "conn_id" not in combined and "secret=" not in combined


def test_unknown_sdk_payloads_and_tracebacks_are_not_logged(sdk_logging, caplog, capsys):
    bot._install_safe_sdk_logging()
    sdk_logger = logging.getLogger("Lark")
    with caplog.at_level(logging.INFO, logger=bot.logger.name):
        sdk_logger.info("receive message, payload: private-event-header-and-customer-text")
        try:
            raise RuntimeError("private-provider-error")
        except RuntimeError:
            sdk_logger.exception("handle message failed: private-payload")
        logging.getLogger("websockets.client").error("wss://private.invalid?credential=private-key")
    output = capsys.readouterr()
    assert caplog.records == []
    assert "private" not in output.out + output.err + caplog.text


def test_safe_sdk_logger_replacement_is_idempotent(sdk_logging):
    bot._install_safe_sdk_logging()
    bot._install_safe_sdk_logging()
    assert len(logging.getLogger("Lark").handlers) == 1
    assert isinstance(logging.getLogger("Lark").handlers[0], bot.SafeSdkConnectionHandler)
