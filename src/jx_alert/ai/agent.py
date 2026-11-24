"""DeepSeek as a SparkX MCP agent, the same way WorkBuddy, Claude or Codex use SparkX.

The model receives the SparkX read tools exactly as the server lists them (name,
description, input schema), the server's own instructions, and the official
read-only Skills through ``read_skill``. Every tool result is passed back as
SparkX returned it; only a result longer than MAX_RESULT_CHARS is cut, with a
note asking for a narrower query. Write tools are never offered (see
sparkx.client.read_only), so the agent can only read.
"""
from contextlib import AsyncExitStack
import hashlib
import logging
import threading
import time
from zoneinfo import ZoneInfo

import anyio

from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import agent_tools, call_raw, open_session

_LOG = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
MARKETPLACE = ZoneInfo("America/Los_Angeles")
MAX_RESULT_CHARS = 150_000
CATALOG_TTL = 30 * 60
_WEEKDAYS = "一二三四五六日"
_catalog = {}
_catalog_lock = threading.Lock()

READ_SKILL = {"type": "function", "function": {
    "name": "read_skill",
    "description": ("读取已安装的 SparkX 官方 Skill 文件（SKILL.md 或其 references/ 下的参考文件）。调用 SparkX 工具前，"
                    "先读取对应 Skill 的 SKILL.md，并按其中要求读取参考文件；长文件按返回的 offset 分段继续读取。"),
    "parameters": {"type": "object", "properties": {
        "skill": {"type": "string", "description": "Skill 名称，例如 sparkx-query-ads-performance"},
        "file": {"type": "string", "description": "Skill 内的相对路径，默认 SKILL.md，例如 references/platform-notes.md"},
        "offset": {"type": "integer", "description": "从第几个字符开始读取，默认 0"}},
        "required": ["skill"]}}}


def function_spec(tool):
    """The MCP tool as a DeepSeek function, unchanged except the JSON-Schema dialect marker."""
    schema = dict(tool.inputSchema) if isinstance(getattr(tool, "inputSchema", None), dict) else {}
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return {"type": "function", "function": {"name": tool.name,
                                             "description": tool.description or tool.title or tool.name,
                                             "parameters": schema}}


def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cached_catalog(token):
    with _catalog_lock:
        entry = _catalog.get(_token_key(token))
    if entry and time.monotonic() - entry["at"] < CATALOG_TTL:
        return entry
    return None


def remember_catalog(token, instructions, specs):
    with _catalog_lock:
        _catalog[_token_key(token)] = {"at": time.monotonic(), "instructions": instructions, "specs": specs}


def clip(text):
    text = text if isinstance(text, str) else str(text)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (text[:MAX_RESULT_CHARS] + f"\n[结果过长，只保留了前 {MAX_RESULT_CHARS} 个字符（原始 {len(text)} 字符）。"
            "请缩小日期范围、减少字段、加筛选或分页后重新查询，不要把这部分当作完整结果。]")


# ---------------------------------------------------------------- prompt context

def clock_text(now):
    beijing, local = now.astimezone(BEIJING), now.astimezone(MARKETPLACE)
    return (f"北京时间 {beijing:%Y-%m-%d %H:%M}（星期{_WEEKDAYS[beijing.weekday()]}）；"
            f"美西（America/Los_Angeles）{local:%Y-%m-%d %H:%M}（星期{_WEEKDAYS[local.weekday()]}）")


def directory_table(roster, directory, *, only_monitored=False):
    """The JX store directory as a Markdown table: names, sites and the operators set in the console."""
    if not roster:
        return "（尚未读取到 SparkX 授权店铺列表）"
    names = directory_module.display_names(roster)
    lines = ["| profileId | 店铺 | 简称 | 站点 | 类型 | 运营 | 纳入通知 |", "|---|---|---|---|---|---|---|"]
    rows = sorted(roster.items(), key=lambda item: (item[1].get("country") != "US", names[item[0]]))
    for key, profile in rows:
        monitored = directory_module.monitored(directory, key)
        if only_monitored and not monitored:
            continue
        cells = [key, profile.get("name") or key, names[key], profile.get("country") or "—",
                 profile.get("store_type") or "—", directory_module.operator_label(directory, key),
                 "是" if monitored else "否"]
        lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in cells) + " |")
    return "\n".join(lines)


# Owner decision (Nov 2026): SparkX's data and its official Skills come first, JX's own task texts second.
PRIORITY = (
    "要求的优先级（有冲突时按顺序取舍）：\n"
    "1. 第一优先级：SparkX 返回的数据，以及 SparkX 官方 Skill 和结果 meta.hint 的要求——查询方法、指标口径、"
    "数据解读，以及它们要求向读者说明的事项（例如最近几天的广告订单、广告销售额和 ACOS 是暂定值，多店金额是 USD，"
    "分页没取完或数据缺失、不足以下结论，相关不等于因果）。Skill 里的示例报告格式只是示意，不算这里的要求。\n"
    "2. 第二优先级：JX 的任务说明和输出要求——写哪些内容、结构、表格和列、篇幅和措辞；"
    "在不违背第 1 条的前提下照做，冲突时以第 1 条为准。\n"
    "3. 从简：第 1 条要求说明的事项用一句短话或在表格里标注，不展开；不写数据来源、生成方式、查询次数和查询过程。"
)


def system_prompt(*, now, tool_names, skills_index, roster, directory, instructions="", mode="report",
                  only_monitored=False, sparkx_note=None):
    tools = "、".join(tool_names) if tool_names else (
        f"（SparkX 暂时不可用：{sparkx_note}）" if sparkx_note else "（SparkX 未配置，暂不能查询广告数据）")
    parts = [
        "你是 JX 公司的亚马逊广告分析助手，由 DeepSeek 驱动，通过 SparkX AI MCP 查询广告数据。"
        "你的工作方式与 WorkBuddy、Claude 等接入 SparkX MCP 的 Agent 相同：按 SparkX 官方 Skill 的方法查询和分析，"
        "结论以 SparkX 返回的数据为准。",
        f"当前时间：{clock_text(now)}。JX 所有店铺的报表时区为美西：「今天」「昨天」默认指美西日期，"
        "美西今天的数据仍在累计；近 7–14 天的销售和订单仍会因归因回补。",
        "工具与边界：\n"
        f"- 可用的 SparkX 只读工具：{tools}；另有 read_skill 读取官方 Skill。\n"
        "- 你只能查询，无法修改任何广告、预算、出价、托管组或店铺设置。用户要求修改时，说明这里只能查询，"
        "可以给出具体建议，请运营在 SparkX 或亚马逊广告后台操作；不要声称已经执行了任何修改。\n"
        "- 调用 SparkX 工具前，先用 read_skill 读取对应的官方 Skill（get_ads_perf → sparkx-query-ads-performance，"
        "get_entity_metadata → sparkx-query-entity-metadata，get_operation_log → sparkx-query-operation-log），"
        "并按 Skill 的要求读取参考文件（例如 references/platform-notes.md）；同一次对话中读过的内容不必重复读取。\n"
        "- 按 Skill 和工具说明构造查询，遵守每次结果 meta.hint 里的提示；分页没取完不要当作全部；"
        "缺失的字段和查不到的数据不是 0；比率由基础指标重新计算，不对比率求平均。\n"
        "- 回答里的数字必须来自工具结果或由其直接计算，不要编造。",
        PRIORITY,
    ]
    if skills_index:
        parts.append("已安装的 SparkX 官方 Skills（只读）：\n" + skill_store.prompt_index(skills_index))
    else:
        parts.append("SparkX 官方 Skills 暂未安装：请严格按工具说明调用，遇到参数错误时按错误信息修正。")
    parts.append("JX 店铺目录（运营由管理员在管理台指定，未指定的写「未映射运营」；「纳入通知」=否的店铺不进入定时通知）：\n"
                 + directory_table(roster, directory, only_monitored=only_monitored))
    if instructions:
        parts.append("SparkX MCP 服务说明（原文）：\n" + instructions)
    if mode == "chat":
        parts.append(
            "对话方式：你在飞书里与同事自由交流，可以回答任何问题，不限于广告；只有需要数据时才调用 SparkX 工具。"
            "用自然、简洁的中文回答，先说结论；SparkX 和 Skill 要求说明的事项一句话带过，"
            "不介绍数据来源、查询过程和分析方法，除非对方问起；"
            "展示数据时用 Markdown 表格（每张不超过 20 行）。"
            "不要输出代码块或 JSON，不要 @ 任何人，不要附链接。")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- the loop

class _Link:
    """Opens the SparkX session on first use, so pure conversation never touches SparkX."""

    def __init__(self, stack, token, timeout):
        self.stack, self.token, self.timeout, self.session = stack, token, timeout, None

    async def get(self):
        if self.session is None:
            self.session = await self.stack.enter_async_context(open_session(self.token, self.timeout))
        return self.session


async def catalog(link):
    """(instructions, specs) for this token: cached for 30 minutes, else listed live."""
    found = cached_catalog(link.token)
    if found:
        return found["instructions"], found["specs"]
    session = await link.get()
    specs = [function_spec(tool) for tool in await agent_tools(session)]
    instructions = getattr(session, "jx_instructions", "") or ""
    remember_catalog(link.token, instructions, specs)
    return instructions, specs


async def converse(client, messages, *, link, specs, settings, skills_index, max_tool_calls, max_rounds,
                   max_tokens=8000, on_step=None):
    """Run the tool loop until the model answers. Returns a summary dict (content + evidence)."""
    sparkx_names = {spec["function"]["name"] for spec in specs}
    tools = list(specs) + ([READ_SKILL] if skills_index else [])
    calls, sparkx_calls, skill_reads, rounds = 0, 0, 0, 0
    evidence, used, usage = [], [], {}

    def add_usage():
        for key, value in (client.last_usage or {}).items():
            usage[key] = usage.get(key, 0) + value

    async def ask(choice):
        reply = await anyio.to_thread.run_sync(lambda: client.chat(
            messages, tools=tools or None, tool_choice=choice, max_tokens=max_tokens))
        add_usage()
        return reply

    reply = None
    while rounds < max_rounds:
        rounds += 1
        reply = await ask("auto" if calls < max_tool_calls else "none")
        messages.append(reply["message"])
        _LOG.info("Agent round %d: %s", rounds, ", ".join(call["name"] for call in reply["tool_calls"]) or "answer")
        if not reply["tool_calls"]:
            break
        for request in reply["tool_calls"]:
            name, arguments = request["name"], request["arguments"]
            calls += 1
            if calls > max_tool_calls:
                output = "本次可用的工具调用次数已用完，请根据已获得的结果直接给出最终回答。"
            elif name == "read_skill" and skills_index:
                skill_reads += 1
                output = skill_store.read(settings, skills_index, str(arguments.get("skill") or ""),
                                          str(arguments.get("file") or "SKILL.md"), arguments.get("offset") or 0)
            elif name in sparkx_names:
                sparkx_calls += 1
                used.append(name)
                if on_step:
                    on_step(name)
                text, failed = await call_raw(await link.get(), name, arguments)
                evidence.append(text)
                output = clip(text if not failed or text else "工具返回错误（无详细信息）。")
            else:
                output = f"工具 {name[:64]} 不可用。可用工具：" + "、".join(sorted(sparkx_names | {"read_skill"}))
            messages.append({"role": "tool", "tool_call_id": request["id"], "content": output})
        reply = None
    if reply is None or reply["tool_calls"]:
        messages.append({"role": "user", "content": "请停止查询，直接根据已经获得的结果给出最终回答。"})
        reply = await ask("none")
        messages.append(reply["message"])
    return {"content": reply["content"], "sparkx_calls": sparkx_calls, "skill_reads": skill_reads,
            "rounds": rounds, "tools_used": used, "evidence": evidence, "usage": usage,
            "model": getattr(client, "model", None), "thinking": getattr(client, "thinking", None)}


async def run_task(settings, client, *, token, messages_for, now, directory, max_tool_calls, max_rounds,
                   max_tokens=8000, need_roster=True, on_roster=None, skills_index=None, roster=None,
                   optional_sparkx=False):
    """One agent run in one SparkX session. ``messages_for(context)`` builds the conversation once
    the roster, tool catalog and Skills are known; returns (summary, context).

    With ``optional_sparkx`` (chat), an unreachable SparkX leaves the model without data tools
    instead of failing the conversation.
    """
    from jx_alert.sparkx.client import SparkXError, authorized_profiles

    timeout = float(getattr(settings, "sparkx_timeout", 30.0))
    async with AsyncExitStack() as stack:
        link = _Link(stack, token, timeout)
        specs, instructions, unavailable = [], "", None
        if token:
            try:
                instructions, specs = await catalog(link)
            except Exception as exc:  # noqa: BLE001 - MCP wraps transport failures in exception groups.
                if not optional_sparkx:
                    raise
                from jx_alert.sparkx.client import failure_code

                unavailable = exc.message if isinstance(exc, SparkXError) else SparkXError(
                    "SPARKX_" + failure_code(exc)).message
                link.session = None
            if need_roster:
                roster = await authorized_profiles(await link.get())
                if on_roster:
                    on_roster(roster)
        context = {"roster": roster or {}, "directory": directory, "instructions": instructions,
                   "tool_names": [spec["function"]["name"] for spec in specs], "skills": skills_index, "now": now,
                   "sparkx_unavailable": unavailable}
        messages = messages_for(context)
        summary = await converse(client, messages, link=link, specs=specs, settings=settings,
                                 skills_index=skills_index, max_tool_calls=max_tool_calls, max_rounds=max_rounds,
                                 max_tokens=max_tokens)
        return summary, context


def evidence_contains(summary, value):
    """True when a SparkX result returned this exact token (used to reject invented campaign IDs)."""
    needle = str(value)
    return bool(needle) and any(needle in text for text in summary.get("evidence", []))
