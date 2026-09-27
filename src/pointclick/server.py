"""pointclick: jev's compact indexed snapshot for the fast path, Playwright for everything else."""

import asyncio
import functools
import json
import os
import re
import struct
from importlib.metadata import version
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

SNAPSHOT = (Path(__file__).parent / "snapshot.js").read_text()
OPS = {"click": "CLICK", "fill": "TYPE", "select": "SELECT"}
VALUE_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
FINGERPRINT = (
    "() => [location.href, document.title, document.body?.innerText.length,"
    " document.querySelectorAll('a,button,input,select,textarea,[role]').length].join('|')"
)
# Two animation frames, or 150 ms, whichever first.
SETTLE = (
    "() => new Promise(r => { let n = 0; const f = () => ++n >= 2 ? r() : requestAnimationFrame(f);"
    " requestAnimationFrame(f); setTimeout(r, 150); })"
)

# After an action: done once the DOM has been still for QUIET s and no request younger than YOUNG s is
# pending, or after CAP s. Older requests (long-polls, streams) don't hold it up.
QUIET, YOUNG, CAP = 0.15, 0.5, 2.0
IGNORED_REQUESTS = {"image", "media", "font"}
DESKTOP = {
    "viewport": {"width": 1280, "height": 800},
    "device_scale_factor": 1,
    "is_mobile": False,
    "has_touch": False,
    "color_scheme": "light",
}

mcp = MCPServer("pointclick", version=version("pointclick"))
S = {
    "pw": None,
    "browser": None,
    "ctx": None,
    "page": None,
    "index": {},
    "console": [],
    "fp": None,
    "notes": [],
    "inflight": {},
    "device": DESKTOP,
    "headless": os.environ.get("HEADED") != "1",
}


# Clients may send tool calls in parallel; one browser and one current page serve them in turn.
_lock = asyncio.Lock()


def _one_at_a_time(tool):
    @functools.wraps(tool)
    async def run(*args, **kwargs):
        async with _lock:
            return await tool(*args, **kwargs)

    return run


def _track(page):
    # Popups (OAuth, new tabs) become the current page.
    S["page"] = page
    page.on("console", lambda m: _log(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: _log(f"[pageerror] {e}"))
    page.on("request", _request_started)
    page.on("requestfinished", _request_done)
    page.on("requestfailed", _request_done)


def _request_started(request):
    if request.resource_type not in IGNORED_REQUESTS:
        S["inflight"][request] = asyncio.get_running_loop().time()


def _request_done(request):
    S["inflight"].pop(request, None)


def _log(line):
    S["console"].append(line)
    del S["console"][:-300]


async def _launch(pw):
    # Installed Chrome first; Playwright's bundled Chromium when it's missing.
    headless = S["headless"]
    channel = os.environ.get("BROWSER_CHANNEL", "chrome")
    if channel == "chromium":
        return await pw.chromium.launch(headless=headless)
    try:
        return await pw.chromium.launch(channel=channel, headless=headless)
    except PWError as e:
        try:
            return await pw.chromium.launch(headless=headless)
        except PWError as e2:
            raise RuntimeError(
                f"No browser. {channel!r}: {str(e).splitlines()[0]} Bundled Chromium: {str(e2).splitlines()[0]} "
                "Install Chrome, or run: playwright install chromium"
            ) from None


async def _new_context(state=None):
    S["ctx"] = await S["browser"].new_context(**S["device"], storage_state=state)
    S["ctx"].on("page", _track)


async def _page():
    if S["browser"] and not S["browser"].is_connected():
        # Someone closed the window.
        S.update(browser=None, ctx=None, page=None, index={}, fp=None, inflight={})
    if S["page"] and not S["page"].is_closed():
        return S["page"]
    if not S["pw"]:
        S["pw"] = await async_playwright().start()
    if not S["browser"]:
        S["browser"] = await _launch(S["pw"])
        await _new_context()
    open_pages = [p for p in S["ctx"].pages if not p.is_closed()]
    if open_pages:
        S["page"] = open_pages[-1]
        return S["page"]
    return await S["ctx"].new_page()


def _device(spec, current):
    m = re.fullmatch(r"(?:(\d+)x(\d+)(?:@(\d+(?:\.\d+)?))?)? ?(mobile)? ?(dark|light)?", " ".join(spec.lower().split()))
    if not m or not any(m.groups()):
        raise ValueError(f'device looks like "440x956@3 mobile dark", not {spec!r}')
    w, h, scale, mobile, scheme = m.groups()
    device = {**current, "color_scheme": scheme or current["color_scheme"]}
    if w:
        scale = float(scale or 1)
        device.update(
            viewport={"width": int(w), "height": int(h)},
            device_scale_factor=int(scale) if scale.is_integer() else scale,
            is_mobile=bool(mobile),
            has_touch=bool(mobile),
        )
    elif mobile:
        device.update(is_mobile=True, has_touch=True)
    return device


async def _session_storage():
    # storage_state() leaves sessionStorage out, and some apps keep their login there.
    page = S["page"]
    if not page or page.is_closed() or not page.url.startswith("http"):
        return None
    try:
        return page.url, await page.evaluate("() => Object.entries(sessionStorage)")
    except PWError:
        return None


async def _restore_session_storage(saved):
    if not saved or not saved[1]:
        return
    url, items = saved
    page = await _page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.evaluate("items => { for (const [k, v] of items) sessionStorage.setItem(k, v); }", items)


async def _set_device(device):
    # Scale and touch are fixed per context, so a new one takes over the old one's cookies and storage.
    if device == S["device"]:
        return
    S["device"] = device
    if not S["ctx"]:
        return
    state = await S["ctx"].storage_state(indexed_db=True)
    session = await _session_storage()
    await S["ctx"].close()
    S.update(page=None, index={}, fp=None, inflight={})
    await _new_context(state)
    await _restore_session_storage(session)


def _pixels(data):
    if data[:4] == b"\x89PNG":
        return struct.unpack(">II", data[16:24])
    i = 2
    while i + 9 < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + length
    return 0, 0


async def _settle(page):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
        await page.evaluate(SETTLE)
    except (PWError, PWTimeout):
        pass


async def _frame_snapshot(frame, full):
    if frame != frame.page.main_frame:
        box = await (await frame.frame_element()).bounding_box()
        if not box or not box["width"] or not box["height"]:
            return None
        vp = frame.page.viewport_size
        if not full and (box["y"] >= vp["height"] or box["y"] + box["height"] <= 0):
            return None
    return await frame.evaluate(SNAPSHOT, full)


async def _snapshot(full):
    page = await _page()
    for attempt in range(40):
        try:
            frames = []
            for frame in page.frames:
                if frame.is_detached():
                    continue
                try:
                    snap = await _frame_snapshot(frame, full)
                except PWError:
                    if frame == page.main_frame:
                        raise
                    continue
                if snap:
                    frames.append((frame, snap))
            if frames and frames[0][0] == page.main_frame:
                return page, frames
        except PWError:
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError("Page never settled")


def _norm(label):
    return " ".join(label.split())


def _render(page, frames, full):
    S["index"] = {}
    main = frames[0][1]
    lines = [f"url: {main['url']}", f"title: {main['title']}"]
    n = 0
    for frame, snap in frames:
        if frame != page.main_frame:
            lines.append(f"-- frame {snap['url'][:80]} --")
        nodes = {}
        for a in snap["actions"]:
            if a["kind"] not in OPS:
                continue
            e = nodes.get(a["node"])
            if e is None:
                n += 1
                label = _norm(a["label"].split(" → ")[0])
                e = nodes[a["node"]] = {"i": str(n), "a": a, "label": label, "ops": [], "options": []}
                S["index"][e["i"]] = {"frame": frame, "node": a["node"], "label": label, "options": e["options"]}
            if OPS[a["kind"]] not in e["ops"]:
                e["ops"].append(OPS[a["kind"]])
            if a["kind"] == "select":
                e["options"].append((a["value"], _norm(a["label"].split(" → ", 1)[-1])))
        for e in nodes.values():
            a = e["a"]
            bits = [f"[{e['i']}]", a["role"], repr(e["label"][:120])]
            value = a.get("current_value") if a["kind"] == "select" else a.get("value")
            if value and a["role"] in VALUE_ROLES:
                bits.append(f"value={value[:80]!r}")
            bits += [k for k in ("checked", "selected", "expanded") if a.get(k) == "true"]
            bits.append(f"<{','.join(e['ops'])}>")
            lines.append(" ".join(bits))
            for k, (_, label) in enumerate(e["options"], 1):
                lines.append(f"    [{e['i']}:{k}] {label}")
        if snap.get("omitted_actions"):
            lines.append(f"({snap['omitted_actions']} more elements omitted)")
    scroll = main["scroll"]
    if not full and scroll["y"] + main["h"] < scroll["height"] - 2:
        lines.append(f"(more below: {scroll['height'] - scroll['y'] - main['h']}px; SCROLL_DOWN or observe(full=true))")
    text = (main.get("text") or "").strip()
    if text:
        lines += ["", "text:", text[: 4000 if full else 1500]]
    return "\n".join(lines)


async def _fingerprint(page):
    try:
        return await page.evaluate(FINGERPRINT)
    except PWError:
        return None


async def _observe(full=False):
    page, frames = await _snapshot(full)
    S["fp"] = await _fingerprint(page)
    return _render(page, frames, full)


async def _quiet(page):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CAP
    last, changed = await _fingerprint(page), loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        fp, now = await _fingerprint(page), loop.time()
        if fp != last:
            last, changed = fp, now
        elif now - changed >= QUIET and not any(now - t < YOUNG for t in S["inflight"].values()):
            return


async def _wait_for_change(page, limit=3.0):
    # Against the last observe, so a change that already landed returns at once.
    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        if await _fingerprint(page) != S["fp"]:
            return
        await asyncio.sleep(0.1)


class Stale(Exception):
    pass


async def _resolve(target):
    """Index from the last observe ("3", or "3:2" for a select option), or any Playwright selector."""
    page = await _page()
    idx, _, opt = target.partition(":")
    if idx.isdigit() and (not opt or opt.isdigit()):
        entry = S["index"].get(idx)
        if not entry:
            raise Stale(f"No element [{idx}] in the last observe.")
        el = await _element(entry)
        if el is None:
            # Re-rendered (framework swapped the node). Only follow it when the label is unambiguous.
            await _observe()
            same = [k for k, v in S["index"].items() if v["label"] == entry["label"] and v["frame"] == entry["frame"]]
            if len(same) != 1:
                raise Stale(f"[{idx}] is gone from the page.")
            idx, entry = same[0], S["index"][same[0]]
            el = await _element(entry)
            if el is None:
                raise Stale(f"[{idx}] is gone from the page.")
            S["notes"].append(f"The element was re-rendered; used [{idx}] {entry['label']!r}.")
        option = entry["options"][int(opt) - 1][0] if opt else None
        return el, option
    return page.locator(target).first, None


async def _element(entry):
    handle = await entry["frame"].evaluate_handle(
        "id => { const e = window.__jevFast?.nodes.get(id); return e?.isConnected ? e : null; }", entry["node"]
    )
    return handle.as_element()


async def _run(coro):
    page = await _page()
    S["notes"] = []
    before = await _fingerprint(page)
    try:
        await coro
    except Stale as e:
        return f"Stale: {e} Fresh table:\n\n" + await _observe()
    except ValueError as e:
        return f"Error: {e}\n\n" + await _observe()
    except (PWError, PWTimeout) as e:
        if before is None or await _fingerprint(page) == before:
            return f"Failed: {str(e).splitlines()[0][:300]}\n\n" + await _observe()
        S["notes"].append(
            "The action timed out, but the page changed since. Check whether it took effect before retrying."
        )
    await _settle(page)
    await _quiet(page)
    table = await _observe()
    return "".join(f"Note: {n}\n" for n in S["notes"]) + ("\n" if S["notes"] else "") + table


@mcp.tool()
@_one_at_a_time
async def navigate(url: str, device: str = "") -> str:
    """Open url and return the indexed element table.
    device: "WxH@scale mobile dark", e.g. "440x956@3 mobile" (touch); "dark"/"light" alone keeps the size.
      Keeps cookies, storage; closes tabs."""
    if device:
        await _set_device(_device(device, S["device"]))
    page = await _page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _settle(page)
    await _quiet(page)
    return await _observe()


@mcp.tool()
@_one_at_a_time
async def observe(full: bool = False) -> str:
    """Indexed table of interactive elements (all frames) plus visible text.
    Default: viewport only (compact). full=true: whole page."""
    return await _observe(full)


@mcp.tool()
@_one_at_a_time
async def act(operation: str, target: str = "", text: str = "") -> str:
    """Do one thing, then return the fresh table.
    operation: CLICK | TYPE | SELECT | PRESS | HOVER | SCROLL_DOWN | SCROLL_UP | WAIT.
      Returns once the page settles (max 2s). If it is still loading after that, WAIT (returns on change, max 3s).
    target: index from the table ("3"; "3:2" picks a SELECT option), or a Playwright selector
      ("input[type=password]", "text=Send", "role=button[name='OK']") for anything not in the table.
    text: TYPE value, SELECT option label (instead of "3:2"), or PRESS key ("Enter")."""
    op = operation.upper().replace("TYPE_TEXT", "TYPE")
    page = await _page()

    async def do():
        if op in ("SCROLL_DOWN", "SCROLL_UP"):
            await page.mouse.wheel(0, 600 if op == "SCROLL_DOWN" else -600)
            return
        if op == "WAIT":
            await _wait_for_change(page)
            return
        if op == "PRESS" and not target:
            await page.keyboard.press(text)
            return
        if not target:
            raise ValueError(f"{op} needs a target.")
        el, option = await _resolve(target)
        if op == "CLICK":
            await el.click(timeout=5000, no_wait_after=True)
        elif op == "TYPE":
            await el.fill(text, timeout=5000)
        elif op == "SELECT" and option:
            await el.select_option(value=option, timeout=5000)
        elif op == "SELECT":
            await el.select_option(label=text, timeout=5000)
        elif op == "PRESS":
            await el.press(text, timeout=5000, no_wait_after=True)
        elif op == "HOVER":
            await el.hover(timeout=5000)
        else:
            raise ValueError(f"Unknown operation {operation!r}.")

    return await _run(do())


@mcp.tool()
@_one_at_a_time
async def upload(target: str, paths: list[str]) -> str:
    """Set files on a file input. target: selector (file inputs are not in the table), e.g. "input[type=file]"."""

    async def do():
        el, _ = await _resolve(target)
        await el.set_input_files(paths, timeout=5000)

    return await _run(do())


@mcp.tool()
@_one_at_a_time
async def screenshot(full_page: bool = False, format: str = "", path: str = "") -> Image | str:
    """JPEG of the current page, or format="png".
    path: save there instead (PNG unless .jpg); returns path and pixel size."""
    page = await _page()
    jpeg_path = path.lower().endswith((".jpg", ".jpeg"))
    fmt = (format or ("jpeg" if not path or jpeg_path else "png")).lower().replace("jpg", "jpeg")
    if fmt not in ("png", "jpeg"):
        raise ValueError(f"format is png or jpeg, not {format!r}")
    quality = (90 if path else 70) if fmt == "jpeg" else None
    data = await page.screenshot(type=fmt, quality=quality, full_page=full_page)
    if not path:
        return Image(data=data, format=fmt)
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    w, h = _pixels(data)
    return f"{out} {w}x{h} {fmt}"


@mcp.tool()
@_one_at_a_time
async def evaluate(js: str, max_chars: int = 6000) -> str:
    """Run JS in the page: an expression, or a function like "() => document.title".
    Returns JSON, cut at max_chars (0: no limit)."""
    page = await _page()
    out = json.dumps(await page.evaluate(js), ensure_ascii=False, default=str)
    if max_chars and len(out) > max_chars:
        return f"{out[:max_chars]}\n(cut at {max_chars} of {len(out)} chars; max_chars=0 returns all of it)"
    return out


@mcp.tool()
@_one_at_a_time
async def console(clear: bool = True) -> str:
    """Console messages and page errors since the last call."""
    lines = S["console"][-100:]
    if clear:
        S["console"].clear()
    return "\n".join(lines) or "(empty)"


@mcp.tool()
@_one_at_a_time
async def show(visible: bool = True) -> str:
    """Show the window so a person can log in; visible=false hides it. Keeps the session."""
    if S["browser"] and S["browser"].is_connected() and S["headless"] == (not visible):
        return await _observe()
    url = S["page"].url if S["page"] and not S["page"].is_closed() else ""
    state = session = None
    if S["browser"] and S["browser"].is_connected():
        state = await S["ctx"].storage_state(indexed_db=True)
        session = await _session_storage()
        await S["browser"].close()
    S.update(browser=None, ctx=None, page=None, index={}, fp=None, inflight={}, headless=not visible)
    if not S["pw"]:
        S["pw"] = await async_playwright().start()
    S["browser"] = await _launch(S["pw"])
    await _new_context(state)
    await _restore_session_storage(session)
    page = await _page()
    if url.startswith("http"):
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _settle(page)
        await _quiet(page)
    return await _observe()


@mcp.tool()
@_one_at_a_time
async def close() -> str:
    """Close the browser. The next call starts a fresh, empty session."""
    if S["browser"] and S["browser"].is_connected():
        await S["browser"].close()
    if S["pw"]:
        await S["pw"].stop()
    S.update(
        pw=None,
        browser=None,
        ctx=None,
        page=None,
        index={},
        console=[],
        fp=None,
        notes=[],
        inflight={},
        device=DESKTOP,
        headless=os.environ.get("HEADED") != "1",
    )
    return "closed"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
