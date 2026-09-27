import json
import sys
from importlib.metadata import version

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.anyio


async def test_over_stdio(origins, tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "pointclick.server"])
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        assert init.server_info.version == version("pointclick")
        listed = (await s.list_tools()).tools
        tools = {t.name for t in listed}
        assert tools == {"navigate", "observe", "act", "upload", "screenshot", "evaluate", "console", "close"}
        # What a model is sent per request, estimated as the README does: JSON characters / 3.5.
        defs = json.dumps(
            [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in listed]
        )
        assert len(defs) / 3.5 < 1000, f"tool definitions are ~{len(defs) / 3.5:.0f} tokens"
        res = await s.call_tool("navigate", {"url": f"{origins[0]}/basic.html"})
        assert "button 'Click me'" in res.content[0].text
        shot = await s.call_tool("screenshot", {})
        assert shot.content[0].type == "image" and shot.content[0].mime_type == "image/jpeg"
        saved = await s.call_tool("screenshot", {"path": str(tmp_path / "a.png")})
        assert saved.content[0].type == "text" and saved.content[0].text.endswith(" 1280x800 png")
        await s.call_tool("close", {})
