"""A real agent (headless Claude Code) does the bench flow from a plain-language task, once per MCP server.

    uv run python bench/agent_race.py --runs 3 --model sonnet

Uses your Claude Code login. Built-in tools are off, so the browser MCP is the only way to the answer.
"""

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))
from serve import serve  # noqa: E402

SERVERS = {
    "pointclick": {"command": sys.executable, "args": ["-m", "pointclick.server"]},
    "playwright": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.81", "--headless", "--isolated", "--viewport-size", "1280x800"],
    },
    "agent-browser": {"command": "npx", "args": ["-y", "agent-browser@0.38.1", "mcp"]},
}
TASK = (
    "Use the browser tools. Open {url}. Sign in with the demo account Ada Lovelace. Go to Tests, search for "
    '"physics", sort A–Z, then open "Physics — Unit 2 review". Reply with only the number of questions that '
    "page shows."
)
ANSWER = "14"


def run(name, url, model, cwd):
    cfg = Path(cwd) / f"{name}.json"
    cfg.write_text(json.dumps({"mcpServers": {name: SERVERS[name]}}))
    cmd = [
        "claude", "-p", TASK.format(url=url),
        "--mcp-config", str(cfg), "--strict-mcp-config",
        "--tools", "", "--allowedTools", f"mcp__{name}",
        "--output-format", "json", "--model", model, "--no-session-persistence",
    ]  # fmt: skip
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=600)
    wall = time.perf_counter() - t0
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        return dict(tool=name, ok=False, error=(p.stdout + p.stderr)[-500:])
    u = out.get("usage", {})
    context = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return dict(
        tool=name,
        ok=ANSWER in str(out.get("result", "")).split(),
        answer=str(out.get("result", ""))[:80],
        s=round(out.get("duration_ms", 0) / 1000, 1),  # time to the answer, as Claude Code reports it
        wall_s=round(wall, 1),
        turns=out.get("num_turns"),
        context_tokens=context,
        output_tokens=u.get("output_tokens", 0),
        cost_usd=out.get("total_cost_usd"),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--poll", type=int, choices=(0, 1), default=0)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    httpd = serve()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/?poll={a.poll}#/login"
    names = [n for n in SERVERS if not a.only or n in a.only.split(",")]
    rows = []
    with tempfile.TemporaryDirectory() as cwd:  # outside any project: no CLAUDE.md, hooks or MCP of its own
        for i in range(a.runs):
            k = i % len(names)
            for name in names[k:] + names[:k]:
                rows.append({"run": i, **run(name, url, a.model, cwd)})
                print(json.dumps(rows[-1], ensure_ascii=False), file=sys.stderr, flush=True)
    httpd.shutdown()

    print(f"\npoll={a.poll}, {a.runs} runs, model {a.model}\n")
    print("| | correct | median time | turns | context tokens | output tokens |")
    print("|---|---|---|---|---|---|")
    for name in names:
        rs = [r for r in rows if r["tool"] == name and "s" in r]
        ok = sum(r["ok"] for r in rs)
        if not rs:
            print(f"| {name} | 0/{a.runs} | — | — | — | — |")
            continue

        def med(k):
            return statistics.median(r[k] for r in rs)

        print(
            f"| {name} | {ok}/{a.runs} | {med('s'):.1f} s | {med('turns'):g} "
            f"| {med('context_tokens'):,.0f} | {med('output_tokens'):,.0f} |"
        )
    if a.out:
        Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
