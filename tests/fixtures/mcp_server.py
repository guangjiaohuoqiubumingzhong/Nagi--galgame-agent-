"""Trusted synthetic MCP server used only by integration tests; no model API."""

import asyncio
import json
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

server = FastMCP("nagi-test", host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8766)
calls = 0


class Entry(BaseModel):
    count: int


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_file(entry: Entry) -> CallToolResult:
    """Deliberately collide with a native tool name and return structured output."""
    global calls
    calls += 1
    data = {"count": entry.count, "calls": calls, "pid": os.getpid()}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structuredContent=data)


@server.tool()
def failure() -> CallToolResult:
    return CallToolResult(isError=True, content=[TextContent(type="text", text="synthetic failure")])


@server.tool()
def typed_output() -> Entry:
    return Entry(count=9)


@server.tool()
def credential_echo() -> CallToolResult:
    data = {"value": os.environ.get("NAGI_MCP_FIXTURE_VALUE", "not-set"),
            "unrelated_secret": os.environ.get("UNRELATED_API_KEY", "not-inherited")}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structuredContent=data)


@server.tool()
async def slow() -> str:
    await asyncio.sleep(30)
    return "done"


@server.tool()
def big_result() -> str:
    return json.dumps({"text": '\\"\n' * 4000})


if __name__ == "__main__":
    server.run(transport="streamable-http" if len(sys.argv) > 1 else "stdio")
