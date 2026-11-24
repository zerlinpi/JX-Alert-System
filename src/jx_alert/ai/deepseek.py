"""DeepSeek Chat Completions client on the fixed official origin.

https://api-docs.deepseek.com/api/create-chat-completion/
https://api-docs.deepseek.com/guides/tool_calls
https://api-docs.deepseek.com/guides/thinking_mode
Function calling is used only with the read-only tools defined by the caller;
the model never receives credentials and cannot reach any write operation. In
thinking mode the reasoning of every earlier turn is passed back, as the API
requires for tool calls.
"""
import json
import math
import time

import httpx

from jx_alert.ai.deepseek_config import MODELS, DeepSeekConfigError, client_settings, options

ENDPOINT = "https://api.deepseek.com/chat/completions"
# The models take a 1M-token context; agent conversations carry Skill documents and tool results.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_CHARS = 60_000
MAX_TIMEOUT = 300


class DeepSeekError(RuntimeError):
    """A sanitized failure safe for operational logs; never a provider payload."""


def _secret_value(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def _validated_key(value):
    value = _secret_value(value)
    if not isinstance(value, str) or not value.strip():
        raise DeepSeekError("尚未配置DeepSeek凭据。")
    value = value.strip()
    if len(value) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise DeepSeekError("DeepSeek凭据格式无效。")
    return value


def _http_error(status):
    messages = {400: "DeepSeek请求格式未被接受。", 401: "DeepSeek凭据无效。", 402: "DeepSeek账户余额不足。",
                403: "DeepSeek访问被拒绝。", 429: "DeepSeek繁忙，请稍后重试。"}
    return DeepSeekError(messages.get(status, "DeepSeek服务暂时不可用。"))


class DeepSeekClient:
    def __init__(self, api_key, *, model="deepseek-flash", timeout=45.0, thinking=False, transport=None):
        self._api_key = _validated_key(api_key)
        if not isinstance(model, str) or model not in MODELS:
            raise DeepSeekError("DeepSeek模型配置无效，请使用当前官方支持的模型。")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT):
            raise DeepSeekError("DeepSeek超时配置必须介于0和300秒之间。")
        self.model, self.timeout, self.thinking, self._transport = model, float(timeout), bool(thinking), transport
        self.last_usage = {}

    @classmethod
    def from_settings(cls, settings, *, transport=None, minimum_timeout=0):
        try:
            key, model, timeout = client_settings(settings)
            thinking = options(settings)["thinking"]
        except DeepSeekConfigError as exc:
            raise DeepSeekError(exc.detail) from None
        if minimum_timeout and isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            # Background agent runs wait longer than the owner's interactive setting.
            timeout = min(max(float(timeout), float(minimum_timeout)), MAX_TIMEOUT)
        return cls(key, model=model, timeout=timeout, thinking=thinking, transport=transport)

    def chat(self, messages, *, tools=None, tool_choice="auto", json_mode=False, max_tokens=4000, temperature=0.2):
        """One completion. Returns {content, reasoning, tool_calls, finish_reason, message}."""
        payload = {"model": self.model, "messages": messages, "stream": False, "max_tokens": max_tokens}
        if self.thinking:
            payload.update(thinking={"type": "enabled"}, reasoning_effort="high")
        else:
            payload.update(thinking={"type": "disabled"}, temperature=temperature)
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise DeepSeekError("分析数据格式无效。") from None
        if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise DeepSeekError("对话内容过长，请缩小查询范围。")
        try:
            # Official origin is fixed; no redirects or environment-selected proxy.
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False,
                              transport=self._transport) as client:
                for attempt in range(3):
                    response = client.post(ENDPOINT, content=encoded.encode("utf-8"), headers={
                        "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"})
                    if (response.status_code == 429 or 500 <= response.status_code <= 599) and attempt < 2:
                        time.sleep(1.0 + 2.0 * attempt)
                        continue
                    if response.status_code != 200:
                        raise _http_error(response.status_code)
                    return self._result(response, tools_allowed=bool(tools) and tool_choice != "none")
        except httpx.TimeoutException:
            raise DeepSeekError("DeepSeek分析超时，请稍后重试。") from None
        except httpx.HTTPError:
            raise DeepSeekError("DeepSeek连接暂时不可用。") from None
        raise DeepSeekError("DeepSeek服务暂时不可用。")

    def _result(self, response, *, tools_allowed):
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise DeepSeekError("DeepSeek返回内容过大。")
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            finish = choice.get("finish_reason")
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            raw_calls = message.get("tool_calls") or []
            if (message.get("role") != "assistant" or not isinstance(raw_calls, list)
                    or (reasoning is not None and not isinstance(reasoning, str))):
                raise ValueError
            calls = []
            for call in raw_calls:
                function = call["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                if (call.get("type", "function") != "function" or not isinstance(call.get("id"), str)
                        or not isinstance(function.get("name"), str) or not isinstance(arguments, dict)):
                    raise ValueError
                calls.append({"id": call["id"][:100], "name": function["name"][:64], "arguments": arguments})
        except (ValueError, TypeError, KeyError, IndexError):
            raise DeepSeekError("DeepSeek返回格式无效，请稍后重试。") from None
        if calls and not tools_allowed:
            raise DeepSeekError("DeepSeek返回了未请求的工具调用。")
        if finish not in {"stop", "tool_calls"} or (not calls and (not isinstance(content, str) or not content.strip())):
            reason = finish if isinstance(finish, str) and finish.isidentifier() and len(finish) <= 30 else "empty"
            raise DeepSeekError(f"DeepSeek未返回完整结果（{reason}），请稍后重试。")
        if isinstance(content, str) and (len(content) > MAX_OUTPUT_CHARS or self._api_key in content):
            raise DeepSeekError("DeepSeek返回内容不符合要求。")
        usage = data.get("usage") if isinstance(data, dict) else None
        self.last_usage = ({key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                                        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
                            if type(usage.get(key)) is int and usage[key] >= 0} if isinstance(usage, dict) else {})
        assistant = {"role": "assistant", "content": content if isinstance(content, str) else ""}
        if reasoning:
            assistant["reasoning_content"] = reasoning  # Must be passed back on later tool-call turns.
        if calls:
            assistant["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
                for call in calls]
        return {"content": content.strip() if isinstance(content, str) else "", "reasoning": reasoning or "",
                "tool_calls": calls, "finish_reason": finish, "message": assistant}

    def complete_json(self, system, user, *, max_tokens=4000, temperature=0.2):
        """A JSON-object answer (DeepSeek JSON Output); the prompt must mention json."""
        result = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                           json_mode=True, max_tokens=max_tokens, temperature=temperature)
        try:
            value = json.loads(result["content"])
        except (ValueError, TypeError, RecursionError):
            raise DeepSeekError("DeepSeek未返回有效的JSON。") from None
        if not isinstance(value, dict):
            raise DeepSeekError("DeepSeek未返回有效的JSON。")
        return value
