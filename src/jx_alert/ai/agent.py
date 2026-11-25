"""DeepSeek as a SparkX MCP agent, the same way WorkBuddy, Claude or Codex use SparkX.

The model receives the SparkX read tools exactly as the server lists them (name,
description, input schema), the server's own instructions, and the official
Skills through ``read_skill``. Every tool result is passed back as SparkX
returned it; only a result longer than MAX_RESULT_CHARS is cut, with a note
asking for a narrower query. The write tools are offered only in the Feishu bot's
write mode (see sparkx.write_mode), and a write call runs only through a
WriteGate that the caller confirmed; every other run can only read.
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
from jx_alert.sparkx import write_mode
from jx_alert.sparkx.client import WRITE_READS, WRITE_TOOLS, agent_tools, call_raw, open_session

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


def _token_key(token, write=False):
    return hashlib.sha256(token.encode()).hexdigest() + (":write" if write else "")


def cached_catalog(token, write=False):
    with _catalog_lock:
        entry = _catalog.get(_token_key(token, write))
    if entry and time.monotonic() - entry["at"] < CATALOG_TTL:
        return entry
    return None


def remember_catalog(token, instructions, specs, write=False):
    with _catalog_lock:
        _catalog[_token_key(token, write)] = {"at": time.monotonic(), "instructions": instructions, "specs": specs}


MAX_WRITES = 30
BLOCKED_WRITE = ("未执行：修改广告必须先得到对方确认。本轮不要调用写入工具：把要修改的店铺、对象（名称和内部 ID）和改动前后"
                 "列成表，最后写「回复「确认执行」后我再修改」，等对方回复「确认执行」后再调用。")
WRITE_LIMIT_REACHED = "未执行：本轮修改次数已达上限。请把已完成和剩下的改动分别列出，剩下的等对方再次确认。"
WRITE_SWITCHED_OFF = "未执行：「广告写入」已被关闭，不能再修改。请说明哪些改动已经完成、哪些没有执行。"


class WriteGate:
    """Whether write tools may run in this turn, and where every attempt is recorded (owner decision, Nov 2026).

    ``confirmed`` is decided by the caller from the conversation (the requester answered 「确认执行」
    to the previous turn's plan); the model cannot change it. ``still_on()`` is asked again before
    every write call (the owner's switch), so switching writes off stops a running turn too.
    ``record(attempt, tool, request, status, result)`` receives every write call: RUNNING before it
    is submitted, then PREVIEW (SparkX's PENDING_CONFIRMATION step), EXECUTED or FAILED; a call
    that may not run is BLOCKED.
    """

    def __init__(self, *, confirmed, record=None, limit=MAX_WRITES, still_on=None):
        self.confirmed, self.record, self.limit, self.count, self.attempts = bool(confirmed), record, limit, 0, 0
        self.still_on = still_on

    def refusal(self):
        """None when the next write call may run, else the text the model gets instead of a result."""
        if not self.confirmed:
            return BLOCKED_WRITE
        if self.count >= self.limit:
            return WRITE_LIMIT_REACHED
        if self.still_on is not None and not self.still_on():
            return WRITE_SWITCHED_OFF
        return None

    async def note(self, name, arguments, status, text=None, *, attempt=None):
        """Record a new attempt (or update ``attempt``) and return its number. The confirmToken never
        reaches the record: the edit-ads Skill forbids logging it."""
        if attempt is None:
            self.attempts += 1
            attempt = self.attempts
        if self.record is not None:
            request, result = write_mode.redact(dict(arguments)), write_mode.redact_text(text)
            await anyio.to_thread.run_sync(lambda: self.record(attempt, name, request, status, result))
        return attempt


def write_outcome(text, failed):
    """FAILED, PREVIEW (a two-phase write waiting for its confirmToken call) or EXECUTED."""
    if failed:
        return "FAILED"
    return "PREVIEW" if isinstance(text, str) and "PENDING_CONFIRMATION" in text else "EXECUTED"


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


READ_ONLY_RULE = ("- 你只能查询，无法修改任何广告、预算、出价、托管组或店铺设置。用户要求修改时，说明这里只能查询，"
                  "可以给出具体建议，请运营在 SparkX 或亚马逊广告后台操作；不要声称已经执行了任何修改。\n")
# Write mode (owner's 「广告写入」 switch on, the requester is a 负责人). The gate in converse enforces
# the confirmation whatever the model does; these lines tell the model how to work with it.
WRITE_PLAN_RULE = (
    "- 广告写入已开启，对方是负责人：可以按 SparkX 官方写入 Skill（如 sparkx-edit-ads）修改广告，每次修改分两轮。"
    "这一轮只做准备：先读写入 Skill，再用只读工具查清对象（写入要用 get_entity_metadata 返回的内部 ID），"
    "把要改的店铺、对象名称和内部 ID、改动前 → 改动后列成表，说明影响（例如花费、是否不可恢复），"
    "最后一行写「回复「确认执行」后我再修改」。这一轮不要调用写入工具，系统会拦截；不要声称已经修改。\n")
WRITE_EXECUTE_RULE = (
    "- 广告写入已开启，对方刚回复「确认执行」：这是对你上一轮列出的修改计划的明确同意（Skill 要求的确认）。"
    "按写入 Skill 执行，只执行计划里的改动，不增不减；返回 PENDING_CONFIRMATION 时核对预览与计划一致，"
    "再在 request 里加 confirmToken 原样调用第二次；不一致就停止，说明差异并请对方重新确认。"
    "执行后用只读工具读回核对，列出成功和失败的项；部分成功如实说明，不要自动重试失败项。\n")
WRITE_DENIED_RULE = (
    "- 广告写入只对负责人（总负责人、广告负责人）开放，对方不是负责人：你只能查询和给建议，不能修改任何广告。"
    "对方要求修改时说明这一点，可以把建议的改动列清楚，请负责人在飞书里向你提出；不要声称已经执行了任何修改。\n")


def system_prompt(*, now, tool_names, skills_index, roster, directory, instructions="", mode="report",
                  only_monitored=False, sparkx_note=None, write=None):
    """``write`` (chat only): None = read-only, "plan" = write mode, preparing; "execute" = confirmed turn."""
    tools = "、".join(tool_names) if tool_names else (
        f"（SparkX 暂时不可用：{sparkx_note}）" if sparkx_note else "（SparkX 未配置，暂不能查询广告数据）")
    rule = {"plan": WRITE_PLAN_RULE, "execute": WRITE_EXECUTE_RULE, "denied": WRITE_DENIED_RULE}.get(write,
                                                                                                    READ_ONLY_RULE)
    parts = [
        "你是 JX 公司的亚马逊广告分析助手，由 DeepSeek 驱动，通过 SparkX AI MCP 查询广告数据。"
        "你的工作方式与 WorkBuddy、Claude 等接入 SparkX MCP 的 Agent 相同：按 SparkX 官方 Skill 的方法查询和分析，"
        "结论以 SparkX 返回的数据为准。",
        f"当前时间：{clock_text(now)}。JX 所有店铺的报表时区为美西：「今天」「昨天」默认指美西日期，"
        "美西今天的数据仍在累计；近 7–14 天的销售和订单仍会因归因回补。",
        "工具与边界：\n"
        f"- 可用的 SparkX {'工具' if write in {'plan', 'execute'} else '只读工具'}：{tools}；另有 read_skill 读取官方 Skill。\n"
        + rule +
        "- 调用 SparkX 工具前，先用 read_skill 读取对应的官方 Skill（get_ads_perf → sparkx-query-ads-performance，"
        "get_entity_metadata → sparkx-query-entity-metadata，get_operation_log → sparkx-query-operation-log），"
        "并按 Skill 的要求读取参考文件（例如 references/platform-notes.md）；同一次对话中读过的内容不必重复读取。\n"
        "- 按 Skill 和工具说明构造查询，遵守每次结果 meta.hint 里的提示；分页没取完不要当作全部；"
        "缺失的字段和查不到的数据不是 0；比率由基础指标重新计算，不对比率求平均。\n"
        "- 回答里的数字必须来自工具结果或由其直接计算，不要编造。",
        PRIORITY,
    ]
    if skills_index:
        parts.append("已安装的 SparkX 官方 Skills" + ("（含写入 Skill）" if write in {"plan", "execute"} else "（只读）")
                     + "：\n" + skill_store.prompt_index(skills_index))
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


async def catalog(link, *, write=False):
    """(instructions, specs) for this token (with the write tools in write mode): cached for 30 minutes."""
    found = cached_catalog(link.token, write)
    if found:
        return found["instructions"], found["specs"]
    session = await link.get()
    specs = [function_spec(tool) for tool in await agent_tools(session, write=write)]
    instructions = getattr(session, "jx_instructions", "") or ""
    remember_catalog(link.token, instructions, specs, write)
    return instructions, specs


async def converse(client, messages, *, link, specs, settings, skills_index, max_tool_calls, max_rounds,
                   max_tokens=8000, on_step=None, write_gate=None):
    """Run the tool loop until the model answers. Returns a summary dict (content + evidence).

    ``evidence[i]`` is the raw text SparkX returned for the call ``tools_used[i]`` made with
    the arguments ``requests[i]``.
    """
    sparkx_names = {spec["function"]["name"] for spec in specs}
    tools = list(specs) + ([READ_SKILL] if skills_index else [])
    calls, sparkx_calls, skill_reads, rounds = 0, 0, 0, 0
    evidence, used, requests, usage = [], [], [], {}

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
            elif name in sparkx_names and name in WRITE_TOOLS:
                # A change to live ads: only in a turn the requester confirmed, never more than the limit.
                arguments = arguments if isinstance(arguments, dict) else {}
                refusal = BLOCKED_WRITE if write_gate is None else await anyio.to_thread.run_sync(write_gate.refusal)
                if refusal is not None:
                    output = refusal
                    if write_gate is not None:
                        await write_gate.note(name, arguments, "BLOCKED")
                else:
                    write_gate.count += 1
                    sparkx_calls += 1
                    used.append(name)
                    attempt = await write_gate.note(name, arguments, "RUNNING")
                    text, failed = await call_raw(await link.get(), name, arguments, write=True)
                    await write_gate.note(name, arguments, write_outcome(text, failed), text, attempt=attempt)
                    output = clip(text if not failed or text else "工具返回错误（无详细信息）。")
            elif name in sparkx_names:
                sparkx_calls += 1
                used.append(name)
                if on_step:
                    on_step(name)
                # The write Skills' helper reads (e.g. get_keyword_list) exist only in write mode.
                text, failed = await call_raw(await link.get(), name, arguments,
                                              write=write_gate is not None and name in WRITE_READS)
                evidence.append(text)
                requests.append(dict(arguments) if isinstance(arguments, dict) else {})
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
            "rounds": rounds, "tools_used": used, "evidence": evidence, "requests": requests, "usage": usage,
            "model": getattr(client, "model", None), "thinking": getattr(client, "thinking", None)}


async def run_task(settings, client, *, token, messages_for, now, directory, max_tool_calls, max_rounds,
                   max_tokens=8000, need_roster=True, on_roster=None, skills_index=None, roster=None,
                   optional_sparkx=False, write=False, write_gate=None):
    """One agent run in one SparkX session. ``messages_for(context)`` builds the conversation once
    the roster, tool catalog and Skills are known; returns (summary, context).

    With ``optional_sparkx`` (chat), an unreachable SparkX leaves the model without data tools
    instead of failing the conversation. ``write`` (the Feishu bot in write mode only) adds the
    write tools; whether a write call may run is decided by ``write_gate`` alone.
    """
    from jx_alert.sparkx.client import SparkXError, authorized_profiles

    timeout = float(getattr(settings, "sparkx_timeout", 30.0))
    async with AsyncExitStack() as stack:
        link = _Link(stack, token, timeout)
        specs, instructions, unavailable = [], "", None
        if token:
            try:
                instructions, specs = await catalog(link, write=write)
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
        tool_names = [spec["function"]["name"] for spec in specs]
        # A Skill is offered only when SparkX offers all of its tools to this token (e.g. market data
        # needs extra scopes); without any tools (SparkX unreachable in chat) the list stays as installed.
        skills_index = skill_store.usable(skills_index, tool_names)
        context = {"roster": roster or {}, "directory": directory, "instructions": instructions,
                   "tool_names": tool_names, "skills": skills_index, "now": now, "sparkx_unavailable": unavailable,
                   "write": bool(write and any(name in WRITE_TOOLS for name in tool_names))}
        messages = messages_for(context)
        summary = await converse(client, messages, link=link, specs=specs, settings=settings,
                                 skills_index=skills_index, max_tool_calls=max_tool_calls, max_rounds=max_rounds,
                                 max_tokens=max_tokens, write_gate=write_gate)
        return summary, context


def evidence_contains(summary, value):
    """True when a SparkX result returned this exact token (used to reject invented campaign IDs)."""
    needle = str(value)
    return bool(needle) and any(needle in text for text in summary.get("evidence", []))
