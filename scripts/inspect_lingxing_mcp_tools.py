"""Inspect the input schemas of Lingxing's read-only performance MCP tools.

This script only calls MCP initialize/tools/list. It does not query business data
or modify any Lingxing records. Credentials are read from .env and never printed.
"""
import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from jx_alert.config import get_settings


TARGET_TOOLS = {
    "customer_store_performance_list",
    "customer_store_target_detail",
    "customer_performance_notice_list",
}


async def inspect_tools() -> None:
    settings = get_settings()
    url = settings.lingxing_mcp_url.strip()
    key = settings.lingxing_mcp_key.get_secret_value()
    if not url or not key:
        raise SystemExit(
            "请先在服务器 .env 配置 LINGXING_MCP_URL 和 LINGXING_MCP_KEY。"
        )

    timeout = httpx.Timeout(30.0, read=300.0)
    try:
        async with httpx.AsyncClient(
            headers={"X-Mcp-Key": key},
            timeout=timeout,
        ) as http_client:
            async with streamable_http_client(
                url,
                http_client=http_client,
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
    except Exception as exc:
        # Never print the configured URL or X-Mcp-Key.
        raise SystemExit(
            f"连接领星 MCP 或读取工具列表失败：{type(exc).__name__}"
        ) from None

    available = {tool.name: tool for tool in result.tools}
    found = sorted(TARGET_TOOLS.intersection(available))
    missing = sorted(TARGET_TOOLS.difference(available))

    print("=== 已发现的店铺绩效工具输入 Schema ===")
    for name in found:
        tool = available[name]
        print(json.dumps(
            tool.model_dump(by_alias=True, exclude_none=True),
            ensure_ascii=False,
            indent=2,
        ))

    if missing:
        print("=== 当前帐号未暴露的目标工具 ===")
        print(json.dumps(missing, ensure_ascii=False, indent=2))
    if not found:
        raise SystemExit(
            "当前 MCP 帐号未暴露目标绩效工具。请检查帐号权限及领星 MCP 工具授权。"
        )


if __name__ == "__main__":
    asyncio.run(inspect_tools())
