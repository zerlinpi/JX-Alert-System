"""In-process stand-ins for the SparkX MCP server (documented envelope) and the DeepSeek client."""
from contextlib import asynccontextmanager
import copy
import json
from types import SimpleNamespace

PROFILES = {
    "101": {"profileId": 101, "profileName": "香港川页", "countryCode": "US", "storeType": "seller",
            "currencyCode": "USD", "timezone": "America/Los_Angeles"},
    "102": {"profileId": 102, "profileName": "Campx-主账号", "countryCode": "US", "storeType": "seller"},
    "103": {"profileId": 103, "profileName": "FITFIZZ-主账号", "countryCode": "US", "storeType": "seller"},
    "104": {"profileId": 104, "profileName": "陈子俊", "countryCode": "US", "storeType": "seller"},
    "105": {"profileId": 105, "profileName": "昌泉", "countryCode": "US", "storeType": "seller"},
    "106": {"profileId": 106, "profileName": "OCTORA", "countryCode": "US", "storeType": "seller"},
    "201": {"profileId": 201, "profileName": "川页-CA", "countryCode": "CA", "storeType": "seller"},
}
SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
          "properties": {"profileIds": {"type": "array", "items": {"type": "integer"}},
                         "userContext": {"type": "string"}}, "additionalProperties": False}


def tool(name, read_only, description=None):
    annotations = None if read_only is None else SimpleNamespace(readOnlyHint=read_only)
    return SimpleNamespace(name=name, title=None, description=description or f"SparkX tool {name}",
                           inputSchema=copy.deepcopy(SCHEMA), annotations=annotations)


TOOLS = [tool("get_user_authorized_context", True), tool("get_ads_perf", True, "Query Amazon advertising performance."),
         tool("get_entity_metadata", True), tool("get_operation_log", True),
         tool("batch_update_ads", None), tool("create_portfolio", False), tool("list_skills", True),
         tool("delete_ai_managed_group", False)]


def result(view, *, error=False):
    return SimpleNamespace(isError=error, structuredContent=view,
                           content=[SimpleNamespace(type="text", text=json.dumps(view, ensure_ascii=False))])


class FakeSparkX:
    """A session: tools/list plus tools/call. ``rows`` maps a factEntity to the rows returned."""

    def __init__(self, rows=None, profiles=None, tools=None):
        self.rows = rows or {}
        self.profiles = PROFILES if profiles is None else profiles
        self.tools = TOOLS if tools is None else tools
        self.calls = []
        self.listed = 0
        self.jx_instructions = ""

    async def list_tools(self, cursor=None):
        self.listed += 1
        return SimpleNamespace(tools=list(self.tools), nextCursor=None)

    async def call_tool(self, name, arguments=None):
        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        if name == "get_user_authorized_context":
            # The live envelope (checked against mcp.sparkx.cn, Nov 2026): profiles under data.
            return result({"isError": False, "toolName": name, "requestId": "req", "data": {
                "userId": 1, "username": "owner", "tenantId": 2, "profiles": list(self.profiles.values())}})
        if name == "get_ads_perf":
            rows = self.rows.get(arguments.get("factEntity"), [])
            return result({"isError": False, "toolName": name, "data": {"rows": rows, "rowCount": len(rows)},
                           "meta": {"page": 1, "pageSize": 100, "hasNextPage": False, "currency": "USD",
                                    "hint": "Limit conclusions to this query's scope."}, "requestId": "req"})
        return result({"isError": False, "toolName": name, "data": {"rows": [], "rowCount": 0}, "meta": {}})


def install(monkeypatch, fake, *modules):
    opened = []

    @asynccontextmanager
    async def session(token, timeout, **_):
        assert token
        opened.append(token)
        yield fake

    for module in modules:
        monkeypatch.setattr(module, "open_session", session)
    fake.opened = opened
    return fake


def reply(content="", *, calls=(), reasoning=""):
    """A DeepSeekClient.chat result; calls are (name, arguments) pairs."""
    tool_calls = [{"id": f"call_{index}", "name": name, "arguments": arguments}
                  for index, (name, arguments) in enumerate(calls)]
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}} for call in tool_calls]
    return {"content": content, "reasoning": reasoning, "tool_calls": tool_calls,
            "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}


class ScriptedDeepSeek:
    """Plays back replies in order and records every request (messages, tools, tool_choice)."""

    model, thinking = "deepseek-v4-pro", True

    def __init__(self, replies, json_replies=()):
        self.replies, self.json_replies = list(replies), list(json_replies)
        self.requests, self.last_usage = [], {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    def chat(self, messages, *, tools=None, tool_choice="auto", json_mode=False, max_tokens=4000, temperature=0.2):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": [spec["function"]["name"] for spec in tools or []],
                              "specs": copy.deepcopy(tools or []), "tool_choice": tool_choice})
        if not self.replies:
            raise AssertionError("no scripted reply left")
        value = self.replies.pop(0)
        return value(messages) if callable(value) else value

    def complete_json(self, system, user, *, max_tokens=4000, temperature=0.2):
        self.requests.append({"system": system, "user": user, "json": True})
        return self.json_replies.pop(0)
