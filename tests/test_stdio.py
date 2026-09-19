import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.anyio


async def test_over_stdio(origins):
    params = StdioServerParameters(command=sys.executable, args=["-m", "pointclick.server"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name for t in (await s.list_tools()).tools}
        assert tools == {"navigate", "observe", "act", "upload", "screenshot", "evaluate", "console", "close"}
        res = await s.call_tool("navigate", {"url": f"{origins[0]}/basic.html"})
        assert "button 'Click me'" in res.content[0].text
        shot = await s.call_tool("screenshot", {})
        assert shot.content[0].type == "image" and shot.content[0].mime_type == "image/jpeg"
        await s.call_tool("close", {})
