"""DeepSeek answers Feishu messages as a SparkX MCP agent and chats freely.

Anything may be asked. SparkX is touched only when the model calls a SparkX tool
(the session opens on that first call), with the same read tools, official
Skills and store directory as the scheduled reports. Recent turns of the same
conversation are passed back, so follow-up questions work like in any chat.
"""
from datetime import datetime, timezone

from jx_alert.ai import agent
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores as directory_module

MAX_TOOL_CALLS = 20
MAX_ROUNDS = 16
MAX_TOKENS = 32000
REQUEST_MINIMUM_TIMEOUT = 90


def answer_question(settings, question, *, history=(), roster=None, now=None, client_factory=None, token=None,
                    directory=None, total_timeout=360.0):
    """{"answer", "provider", "calls"}; raises DeepSeekError / SparkXError with safe messages."""
    from jx_alert.ai.deepseek import DeepSeekClient, DeepSeekError
    from jx_alert.sparkx.client import run
    from jx_alert.sparkx.credentials import effective_token

    now = now or datetime.now(timezone.utc)
    token = effective_token(settings) if token is None else token
    client = (client_factory or (lambda value: DeepSeekClient.from_settings(
        value, minimum_timeout=REQUEST_MINIMUM_TIMEOUT)))(settings)
    directory = directory_module.load(settings) if directory is None else directory
    index = skill_store.ensure(settings) if token else None

    def messages_for(context):
        system = agent.system_prompt(now=now, tool_names=context["tool_names"], skills_index=index,
                                     roster=context["roster"], directory=directory,
                                     instructions=context["instructions"], mode="chat",
                                     sparkx_note=context.get("sparkx_unavailable"))
        turns = []
        for item in history:
            turns += [{"role": "user", "content": item["question"]}, {"role": "assistant", "content": item["answer"]}]
        return [{"role": "system", "content": system}, *turns, {"role": "user", "content": question}]

    async def conversation():
        return await agent.run_task(settings, client, token=token, messages_for=messages_for, now=now,
                                    directory=directory, max_tool_calls=MAX_TOOL_CALLS, max_rounds=MAX_ROUNDS,
                                    max_tokens=MAX_TOKENS, need_roster=False, skills_index=index,
                                    roster=roster or {}, optional_sparkx=True)

    summary, _ = run(conversation, total_timeout=total_timeout, passthrough=(DeepSeekError,))
    return {"answer": summary["content"], "provider": "deepseek", "calls": summary["sparkx_calls"],
            "skill_reads": summary["skill_reads"], "usage": summary["usage"]}
