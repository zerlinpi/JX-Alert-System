"""DeepSeek answers Feishu messages as a SparkX MCP agent and chats freely.

Anything may be asked. SparkX is touched only when the model calls a SparkX tool
(the session opens on that first call), with the same read tools, official
Skills and store directory as the scheduled reports. Recent turns of the same
conversation are passed back, so follow-up questions work like in any chat.

Write mode (owner decision, Nov 2026; see sparkx.write_mode): with the owner's
「广告写入」 switch on, a 负责人 also gets the write tools and every write Skill.
A "plan" turn may only prepare a change (every write call is blocked); only the
"execute" turn that bot_service confirmed may run the writes, at most
agent.MAX_WRITES of them, each one recorded.
"""
from datetime import datetime, timezone

from jx_alert.ai import agent
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx import write_mode

MAX_TOOL_CALLS = 20
MAX_ROUNDS = 16
MAX_TOKENS = 32000
REQUEST_MINIMUM_TIMEOUT = 90
TOTAL_TIMEOUT = 360.0
# A change needs reading the write Skill, the objects and, after the writes, reading them back.
WRITE_TOOL_CALLS = 45
WRITE_ROUNDS = 24
WRITE_TOTAL_TIMEOUT = 600.0
WRITE_MODES = {"plan", "execute"}


def answer_question(settings, question, *, history=(), roster=None, now=None, client_factory=None, token=None,
                    directory=None, total_timeout=None, write=None, record=None):
    """{"answer", "provider", "calls", "writes", "write"}; raises DeepSeekError / SparkXError with safe messages.

    ``write``: None (read-only), "denied" (writes on, the requester is not a 负责人), "plan" or
    "execute" (decided by bot_service, never by the model). ``record(name, request, status, text)``
    receives every write attempt. "write" in the result is the mode the model actually worked in:
    write mode falls back to read-only when SparkX lists no write tool for this token.
    """
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError
    from jx_alert.sparkx.client import run
    from jx_alert.sparkx.credentials import effective_token

    now = now or datetime.now(timezone.utc)
    token = effective_token(settings) if token is None else token
    writing = write in WRITE_MODES and bool(token)
    client = (client_factory or (lambda value: DeepSeekClient.from_settings(
        value, minimum_timeout=REQUEST_MINIMUM_TIMEOUT)))(settings)
    directory = directory_module.load(settings) if directory is None else directory
    index = skill_store.for_agent(settings, write=writing) if token else None
    gate = agent.WriteGate(confirmed=write == "execute", record=record,
                           still_on=lambda: write_mode.enabled(settings)) if writing else None
    used = {}

    def messages_for(context):
        used["write"] = write if context.get("write") else ("denied" if write == "denied" else None)
        system = agent.system_prompt(now=now, tool_names=context["tool_names"], skills_index=context["skills"],
                                     roster=context["roster"], directory=directory,
                                     instructions=context["instructions"], mode="chat",
                                     sparkx_note=context.get("sparkx_unavailable"), write=used["write"])
        turns = []
        for item in history:
            turns += [{"role": "user", "content": item["question"]}, {"role": "assistant", "content": item["answer"]}]
        return [{"role": "system", "content": system}, *turns, {"role": "user", "content": question}]

    async def conversation():
        return await agent.run_task(settings, client, token=token, messages_for=messages_for, now=now,
                                    directory=directory, max_tool_calls=WRITE_TOOL_CALLS if writing else MAX_TOOL_CALLS,
                                    max_rounds=WRITE_ROUNDS if writing else MAX_ROUNDS, max_tokens=MAX_TOKENS,
                                    need_roster=False, skills_index=index, roster=roster or {}, optional_sparkx=True,
                                    write=writing, write_gate=gate)

    timeout = total_timeout or (WRITE_TOTAL_TIMEOUT if writing else TOTAL_TIMEOUT)
    summary, _ = run(conversation, total_timeout=timeout, passthrough=(DeepSeekError,))
    return {"answer": summary["content"], "provider": "deepseek", "calls": summary["sparkx_calls"],
            "skill_reads": summary["skill_reads"], "usage": summary["usage"],
            "writes": gate.count if gate else 0, "write": used.get("write")}
