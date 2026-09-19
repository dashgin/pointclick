"""jev's indexed browser as an MCP server. No TypeSafe: the caller is the brain."""

from jev_ultrafast.browser import Browser, StalePage
from jev_ultrafast.model import action_space
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("jev")
S = {"browser": None, "page": None}


def render(page):
    elements, _, controls = action_space(page["actions"])
    lines = [f"url: {page['url']}", f"title: {page['title']}", ""]
    for e in elements:
        extra = []
        if e.get("value"):
            extra.append(f"value={e['value']!r}")
        for k in ("checked", "selected", "expanded"):
            if e.get(k) not in (None, False):
                extra.append(k)
        ops = ",".join(e["operations"])
        lines.append(f"[{e['index']}] {e.get('role') or '-'} {e['label']!r} {' '.join(extra)} <{ops}>".rstrip())
        for o in e.get("options", []):
            lines.append(f"    [{o['index']}] {o['label']}")
    if controls:
        lines.append("controls: " + ", ".join(controls))
    text = (page.get("text") or "").strip()
    if text:
        lines += ["", "text:", text[:1500]]
    return "\n".join(lines)


def observe():
    S["page"] = S["browser"].observe(screenshot=False)
    return render(S["page"])


@mcp.tool()
def jev_navigate(url: str) -> str:
    """Open url in a fresh tab and return the indexed element table."""
    if S["browser"]:
        S["browser"].close()
    S["browser"] = Browser(url)
    return observe()


@mcp.tool()
def jev_observe() -> str:
    """Re-read the page. Only viewport-visible elements are indexed; scroll for more."""
    return observe()


@mcp.tool()
def jev_act(operation: str, target: str = "", text: str = "") -> str:
    """operation: CLICK | TYPE_TEXT | SELECT | SCROLL_DOWN | SCROLL_UP | WAIT.
    target: element index ("3"), or option index ("3:2") for SELECT. text: for TYPE_TEXT.
    Returns the new element table."""
    page = S["page"]
    _, targets, controls = action_space(page["actions"])
    op = operation.upper()
    if op in controls:
        action = controls[op]
    else:
        action = targets.get(op, {}).get(target)
        if action is None:
            return f"No {op} target {target!r}. Valid: {sorted(targets.get(op, {}))}"
    try:
        S["browser"].act(action, page, text=text or None)
    except StalePage as e:
        return f"Stale: {e}\n\n" + observe()
    return observe()


@mcp.tool()
def jev_close() -> str:
    """Close the tab."""
    if S["browser"]:
        S["browser"].close()
        S["browser"] = None
    return "closed"


if __name__ == "__main__":
    mcp.run()
