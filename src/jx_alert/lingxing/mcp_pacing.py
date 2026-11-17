"""Shared pacing for official MCP reads in the serialized collection worker."""
import asyncio
import threading
import time

_guard = threading.Lock()
_next_request = 0.0


async def paced_call(session, tool, params):
    global _next_request
    with _guard:
        now = time.monotonic()
        due = max(now, _next_request)
        _next_request = due + 0.7  # At most 86 tool calls/minute, including discovery.
    if due > now:
        await asyncio.sleep(due - now)
    return await session.call_tool(tool, params)
