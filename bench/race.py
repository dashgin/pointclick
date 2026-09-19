"""Same scripted driver, several browser MCP servers, one SPA flow. Measures the tools, not a model.

uv run python bench/race.py --runs 6 --poll 0
uv run python bench/race.py --runs 6 --poll 1
JEV_DIR=../jev-ultrafast uv run python bench/race.py   # adds jev (needs bench/jev_mcp.py copied there)
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent / "app"))
from serve import serve  # noqa: E402

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.81"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# (action, pattern, arg). Patterns match the indexed table ('label') and Playwright YAML ("label").
STEPS = [
    ("click", r"Ada Lovelace", None),
    ("click", r"link ['\"]Tests['\"]", None),
    ("type", r"Search tests", "physics"),
    ("select", r"combobox ['\"]Sort", "A–Z"),
    ("click", r"link ['\"]Physics — Unit 2 review", None),
]
DONE = r"['\"]Start live['\"]"  # the detail page's button; content-based so every contender is judged alike
AGENT_BROWSER = "agent-browser@0.38.1"


class Meter:
    def __init__(self):
        self.calls, self.chars, self.polls, self.log, self.trace = 0, 0, 0, [], []


class Indexed:
    """Drives any server that returns jev-style '[n] role 'label'' tables."""

    def __init__(self, s, m, tools):
        self.s, self.m, self.obs, self.t = s, m, "", tools

    async def call(self, tool, args):
        self.m.calls += 1
        c0 = time.perf_counter()
        t = (await self.s.call_tool(tool, args)).content[0].text
        self.m.log.append((tool, round((time.perf_counter() - c0) * 1000)))
        self.m.trace.append(f"{tool} {json.dumps(args, ensure_ascii=False)} -> {t.splitlines()[0][:90]}")
        self.m.chars += len(t)
        self.obs = t
        return t

    async def navigate(self, url):
        await self.call(self.t["navigate"], {"url": url})

    async def observe(self):
        await self.call(*self.t["poll"])

    def find(self, pat):
        for line in self.obs.split("\ntext:")[0].splitlines():
            m = re.match(r"\[(\d+)\]", line)
            if m and re.search(pat, line):
                return m.group(1)

    def url(self):
        m = re.match(r"url: (\S+)", self.obs)
        return m.group(1) if m else ""

    async def act(self, kind, target, arg):
        if kind == "select" and self.t["select_by_option"]:
            target = next(
                re.match(r"\s+\[(\S+)\]", line).group(1)
                for line in self.obs.splitlines()
                if line.startswith("    [") and arg in line
            )
        op = self.t["ops"][kind]
        await self.call(self.t["act"], {"operation": op, "target": target, "text": arg or ""})


class PW:
    def __init__(self, s, m, out):
        self.s, self.m, self.obs, self.out = s, m, "", out

    async def call(self, tool, args):
        self.m.calls += 1
        c0 = time.perf_counter()
        res = await self.s.call_tool(tool, args)
        self.m.log.append((tool, round((time.perf_counter() - c0) * 1000)))
        t = "\n".join(c.text for c in res.content if hasattr(c, "text"))
        if res.is_error:
            raise RuntimeError(f"{tool}: {t[:300]}")
        self.m.chars += len(t)
        # A linked snapshot file is what an agent has to read to get refs, so it counts.
        link = re.search(r"\[Snapshot\]\(([^)]+\.yml)\)", t)
        if link:
            y = next(self.out.rglob(Path(link.group(1)).name)).read_text()
            self.m.chars += len(y)
            t += "\n" + y
        self.obs = t if ("[ref=" in t or "Page URL" in t) else self.obs + "\n" + t
        return t

    async def navigate(self, url):
        await self.call("browser_navigate", {"url": url})

    async def observe(self):
        await self.call("browser_snapshot", {})

    def find(self, pat):
        for line in self.obs.splitlines():
            m = re.search(r"\[ref=(e\d+)\]", line)
            if m and re.search(pat, line):
                return m.group(1)

    def url(self):
        urls = re.findall(r"Page URL: (\S+)", self.obs)
        return urls[-1] if urls else ""

    async def act(self, kind, ref, arg):
        el = {"element": "target", "target": ref}
        if kind == "click":
            await self.call("browser_click", el)
        elif kind == "type":
            await self.call("browser_type", {**el, "text": arg})
        else:
            await self.call("browser_select_option", {**el, "values": [arg]})


class AB:
    """agent-browser: actions return no page, so the agent re-snapshots (-i) to see it."""

    def __init__(self, s, m):
        self.s, self.m, self.obs = s, m, ""

    async def call(self, tool, args):
        self.m.calls += 1
        c0 = time.perf_counter()
        res = await self.s.call_tool(tool, args)
        self.m.log.append((tool, round((time.perf_counter() - c0) * 1000)))
        t = "\n".join(c.text for c in res.content if hasattr(c, "text"))
        if res.is_error:
            raise RuntimeError(f"{tool}: {t[:300]}")
        self.m.trace.append(f"{tool} {json.dumps(args, ensure_ascii=False)} -> {t.splitlines()[0][:90] if t else ''}")
        self.m.chars += len(t)
        return t

    async def navigate(self, url):
        await self.call("agent_browser_open", {"url": url})
        await self.observe()

    async def observe(self):
        self.obs = await self.call("agent_browser_snapshot", {"interactive": True})

    def find(self, pat):
        for line in self.obs.splitlines():
            m = re.search(r"ref=(e\d+)", line)
            if m and re.search(pat, line):
                return "@" + m.group(1)

    async def act(self, kind, ref, arg):
        if kind == "click":
            await self.call("agent_browser_click", {"selector": ref})
        elif kind == "type":
            await self.call("agent_browser_fill", {"selector": ref, "text": arg})
        else:
            await self.call("agent_browser_select", {"selector": ref, "values": [arg]})
        await self.observe()


async def run_agent_browser(url):
    params = StdioServerParameters(command="npx", args=["-y", AGENT_BROWSER, "mcp"])
    return await session(params, lambda s, m: AB(s, m), url, ("agent_browser_close", {}))


async def wait_for(d, pat, test=None, polls=60):
    for _ in range(polls):
        hit = test() if test else d.find(pat)
        if hit:
            return hit
        d.m.polls += 1
        await asyncio.sleep(0.1)
        await d.observe()
    lines = [x for i, x in enumerate(d.m.trace) if i == 0 or x != d.m.trace[i - 1]]
    raise RuntimeError(f"never saw {pat!r}\n" + "\n".join(lines) + "\n--- last page ---\n" + d.obs[:1500])


async def flow(d, url):
    t0 = time.perf_counter()
    await d.navigate(url)
    for kind, pat, arg in STEPS:
        await d.act(kind, await wait_for(d, pat), arg)
    await wait_for(d, DONE)
    return round((time.perf_counter() - t0) * 1000)


async def session(params, make, url, closer):
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        m = Meter()
        d = make(s, m)
        await d.navigate("about:blank")  # launches the browser; not timed
        d.m.__init__()
        ms = await flow(d, url)
        await s.call_tool(*closer)
        return ms, m


async def run_pointclick(url):
    tools = {
        "navigate": "navigate",
        "act": "act",
        "poll": ("act", {"operation": "WAIT"}),
        "ops": {"click": "CLICK", "type": "TYPE", "select": "SELECT"},
        "select_by_option": False,
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "pointclick.server"])
    return await session(params, lambda s, m: Indexed(s, m, tools), url, ("close", {}))


async def run_playwright(url):
    out = Path(tempfile.mkdtemp(prefix="pwmcp-"))
    args = ["-y", PLAYWRIGHT_MCP, "--headless", "--isolated", "--viewport-size", "1280x800", "--output-dir", str(out)]
    try:
        params = StdioServerParameters(command="npx", args=args)
        return await session(params, lambda s, m: PW(s, m, out), url, ("browser_close", {}))
    finally:
        shutil.rmtree(out, ignore_errors=True)


async def run_jev(url):
    jev = Path(os.environ["JEV_DIR"]).resolve()
    prof = tempfile.mkdtemp(prefix="jevprof-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9333", f"--user-data-dir={prof}", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=0.5)
                break
            except OSError:
                await asyncio.sleep(0.1)
        env = {**os.environ, "BU_CDP_URL": "http://127.0.0.1:9333", "BU_NAME": "jevrace"}
        tools = {
            "navigate": "jev_navigate",
            "act": "jev_act",
            "poll": ("jev_observe", {}),
            "ops": {"click": "CLICK", "type": "TYPE_TEXT", "select": "SELECT"},
            "select_by_option": True,
        }
        params = StdioServerParameters(
            command=str(jev / ".venv/bin/python"), args=[str(jev / "jev_mcp.py")], env=env, cwd=str(jev)
        )
        return await session(params, lambda s, m: Indexed(s, m, tools), url, ("jev_close", {}))
    finally:
        chrome.terminate()
        chrome.wait()
        shutil.rmtree(prof, ignore_errors=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--poll", type=int, choices=(0, 1), default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="", help="comma-separated contenders")
    a = ap.parse_args()

    httpd = serve()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/?poll={a.poll}#/login"

    contenders = [("pointclick", run_pointclick), ("playwright", run_playwright), ("agent-browser", run_agent_browser)]
    if os.environ.get("JEV_DIR"):
        contenders.append(("jev", run_jev))
    if a.only:
        contenders = [c for c in contenders if c[0] in a.only.split(",")]
    rows = []
    for i in range(a.runs):
        k = i % len(contenders)
        for name, fn in contenders[k:] + contenders[:k]:
            try:
                ms, m = await fn(url)
                rows.append(
                    dict(run=i, tool=name, ok=True, ms=ms, calls=m.calls, polls=m.polls, chars=m.chars, log=m.log)
                )
            except Exception as e:
                while isinstance(e, BaseExceptionGroup):
                    e = e.exceptions[0]
                rows.append(dict(run=i, tool=name, ok=False, error=f"{type(e).__name__}: {e}"[:1500]))
            print(json.dumps(rows[-1], ensure_ascii=False), file=sys.stderr, flush=True)
    httpd.shutdown()

    print(f"\npoll={a.poll}, {a.runs} runs, {PLAYWRIGHT_MCP}, {AGENT_BROWSER}\n")
    print("| | median | min–max | ok | calls | chars to the model (~tokens) |")
    print("|---|---|---|---|---|---|")
    for name, _ in contenders:
        ok = [r for r in rows if r["tool"] == name and r["ok"]]
        if not ok:
            print(f"| {name} | — | — | 0/{a.runs} | — | — |")
            continue
        ms = [r["ms"] for r in ok]
        chars = statistics.median(r["chars"] for r in ok)
        print(
            f"| {name} | {statistics.median(ms) / 1000:.2f} s | {min(ms) / 1000:.2f}–{max(ms) / 1000:.2f} s "
            f"| {len(ok)}/{a.runs} | {statistics.median(r['calls'] for r in ok):g} "
            f"| {chars:,.0f} (~{chars / 3.5:,.0f}) |"
        )
    if a.out:
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
