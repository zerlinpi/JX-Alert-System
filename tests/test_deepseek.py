"""DeepSeek Chat Completions client: tool calls, JSON output and sanitized failures."""
import json

import httpx
import pytest

from jx_alert.ai.deepseek import ENDPOINT, DeepSeekClient, DeepSeekError
from jx_alert.ai import deepseek_config

KEY = "sk-synthetic-deepseek-key"


def client(handler):
    return DeepSeekClient(KEY, model="deepseek-flash", timeout=5, transport=httpx.MockTransport(handler))


def completion(message, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **message}, "finish_reason": finish}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


def test_tool_calls_are_parsed_and_the_request_disables_thinking():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return completion({"content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "list_stores", "arguments": "{}"}}]}, finish="tool_calls")

    result = client(handler).chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {
        "name": "list_stores", "parameters": {"type": "object", "properties": {}}}}])
    assert seen["url"] == ENDPOINT and seen["auth"] == "Bearer " + KEY
    assert seen["body"]["thinking"] == {"type": "disabled"} and seen["body"]["tool_choice"] == "auto"
    assert result["tool_calls"] == [{"id": "call_1", "name": "list_stores", "arguments": {}}]
    assert result["message"]["tool_calls"][0]["function"]["name"] == "list_stores"


def test_json_mode_and_invalid_json():
    ok = client(lambda request: completion({"content": '{"notes": []}'})).complete_json("写 json", "{}")
    assert ok == {"notes": []}
    with pytest.raises(DeepSeekError):
        client(lambda request: completion({"content": "not json"})).complete_json("写 json", "{}")


@pytest.mark.parametrize("status,message", [(401, "凭据无效"), (402, "余额不足"), (500, "暂时不可用")])
def test_http_failures_are_sanitized(status, message):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "secret provider detail " + KEY}})
    with pytest.raises(DeepSeekError) as exc:
        client(handler).chat([{"role": "user", "content": "hi"}])
    assert message in str(exc.value) and KEY not in str(exc.value) and "secret" not in str(exc.value)


def test_unrequested_tool_calls_truncation_and_key_echo_are_rejected():
    call = {"content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}
    for response in (completion(call, finish="tool_calls"), completion({"content": "部分"}, finish="length"),
                     completion({"content": "密钥是 " + KEY})):
        with pytest.raises(DeepSeekError):
            client(lambda request, response=response: response).chat([{"role": "user", "content": "hi"}])


def test_thinking_mode_sends_effort_and_returns_reasoning_for_passback():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return completion({"content": "", "reasoning_content": "先查店铺", "tool_calls": [{"id": "c1", "type": "function",
                           "function": {"name": "get_ads_perf", "arguments": "{\"factEntity\": \"campaign\"}"}}]},
                          finish="tool_calls")

    thinking = DeepSeekClient(KEY, model="deepseek-v4-pro", timeout=300, thinking=True,
                              transport=httpx.MockTransport(handler))
    result = thinking.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {
        "name": "get_ads_perf", "parameters": {"type": "object", "properties": {}}}}])
    assert seen["body"]["thinking"] == {"type": "enabled"} and seen["body"]["reasoning_effort"] == "high"
    assert "temperature" not in seen["body"]
    assert result["message"]["reasoning_content"] == "先查店铺" and result["reasoning"] == "先查店铺"
    with pytest.raises(DeepSeekError):
        DeepSeekClient(KEY, timeout=301)


def test_console_configuration_is_write_only(settings):
    keyless = settings.model_copy(update={"deepseek_api_key": ""})
    view = deepseek_config.configuration(keyless)
    assert view["configured"] is False and view["thinking"] is True
    saved = deepseek_config.save_configuration(keyless, {"revision": view["revision"], "api_key": KEY,
                                                        "model": "deepseek-flash", "timeout": 200, "thinking": False})
    assert saved["configured"] and saved["key_source"] == "console" and KEY not in json.dumps(saved)
    assert deepseek_config.client_settings(keyless) == (KEY, "deepseek-flash", 200)
    assert deepseek_config.options(keyless) == {"thinking": False} and saved["thinking"] is False
    client = DeepSeekClient.from_settings(keyless, minimum_timeout=250)
    assert client.timeout == 250 and client.thinking is False
    with pytest.raises(deepseek_config.DeepSeekConfigError):
        deepseek_config.save_configuration(keyless, {"revision": saved["revision"], "api_key": "", "model": "deepseek-flash",
                                                     "timeout": 30, "thinking": "yes"})
