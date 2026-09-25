# pointclick

**A browser MCP server in under 1,000 tokens.** The page comes back as a numbered list of what
you can click, type into or select. Your agent points at a number; pointclick clicks it.

[![PyPI](https://img.shields.io/pypi/v/pointclick.svg)](https://pypi.org/project/pointclick/)
[![CI](https://github.com/dashgin/pointclick/actions/workflows/ci.yml/badge.svg)](https://github.com/dashgin/pointclick/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```
url: https://en.wikipedia.org/wiki/Main_Page
title: Wikipedia, the free encyclopedia
[1] link 'Wikipedia The Free Encyclopedia' <CLICK>
[2] searchbox 'Search Wikipedia' <TYPE,CLICK>
[3] button 'Search' <CLICK>
...
(more below: 3156px; SCROLL_DOWN or observe(full=true))

text:
Welcome to
Wikipedia
...
```

```
act("TYPE", "2", "Ada Lovelace")   → the new list, once the page has settled
act("PRESS", "2", "Enter")
```

## Why

Same task, same model (Claude Sonnet 5 in headless Claude Code), one browser MCP each —
sign in, search, sort, open a record, read a number off it. 18 of 18 runs got it right.

| | turns | tokens read | cost per run | time |
|---|---|---|---|---|
| **pointclick** | **7** | **104k** | **$0.056** | **15.8 s** |
| Playwright MCP | 13 | 266k | $0.118 | 27.5 s |
| agent-browser | 12 | 525k | $0.223 | 17.8 s |

Medians. Cost is the API price Claude Code reports; time is noisy at this sample size.

- **Eight tools, under 1k tokens of definitions.** Playwright MCP's are ~5k, Chrome DevTools MCP's
  ~7k, agent-browser's ~18k. Clients that load every tool up front pay that on every turn.
- **Only what you can act on.** No wrapper `div`s, no layout tree: controls, their state, and the
  visible text.
- **Every action returns the settled page.** No follow-up snapshot call, and no waiting for the
  network to go quiet: long-polls and streams don't stall it.

## Quick start

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). Uses your installed Chrome, or
Playwright's Chromium if there is none (`uvx --from pointclick playwright install chromium`).

**Claude Code**

```bash
claude mcp add pointclick -- uvx pointclick
```

**Cursor, Claude Desktop, Windsurf, and other clients** — add to the MCP config:

```json
{
  "mcpServers": {
    "pointclick": { "command": "uvx", "args": ["pointclick"] }
  }
}
```

**VS Code**

```bash
code --add-mcp '{"name":"pointclick","command":"uvx","args":["pointclick"]}'
```

**Codex**

```bash
codex mcp add pointclick -- uvx pointclick
```

### Your first prompt

```
Open news.ycombinator.com, go to the second page, and tell me the top story there.
```

## Tools

| Tool | |
|---|---|
| `navigate(url)` | Open a page, return the list |
| `observe(full=false)` | The list again. Viewport only unless `full` |
| `act(operation, target, text)` | `CLICK` `TYPE` `SELECT` `PRESS` `HOVER` `SCROLL_DOWN` `SCROLL_UP` `WAIT` |
| `upload(target, paths)` | Set files on a file input |
| `screenshot(full_page=false)` | JPEG |
| `evaluate(js, max_chars=6000)` | Run JS in the page, JSON back. A longer result is cut and says so; `0` for all of it |
| `console()` | Console messages and page errors since the last call |
| `close()` | End the session; the next call starts fresh |

`target` is a number from the list (`"3"`, or `"3:2"` for the second option of a select), or any
Playwright selector (`"input[type=password]"`, `"text=Send"`, `"role=button[name='OK']"`).
Password and file inputs are never in the list; reach them by selector.

| Env | |
|---|---|
| `HEADED=1` | Show the window |
| `BROWSER_CHANNEL` | `chrome` (default), `msedge`, or `chromium` for Playwright's bundled build |

## How it works

- **The list** comes from one script that reads every visible control and the visible text in a
  single pass — adapted from [jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
  (Browser Use × TypeSafe). It runs in every frame, cross-origin ones included, and a control that
  only repeats its wrapper's label is listed once.
- **Settling.** After an action, pointclick returns once the page has been still for 150 ms and
  no request younger than 0.5 s is pending — at most 2 s. Older requests (long-polls, streams)
  don't hold it up. If the page is still busy after that, `WAIT` returns the moment it changes.
- **Numbers point at real elements**, not at a position. If a framework re-renders the element,
  pointclick follows it when its label is unique on the page and says so; a duplicate label (two
  "Start" buttons) is refused rather than guessed.
- **A timeout on a page that changed anyway** is reported that way, so the agent checks before
  retrying instead of clicking twice.
- **Playwright underneath** handles selectors, uploads, popups (they become the current page),
  screenshots and the console. Every session is a fresh, isolated browser context.

`server.py` is ~350 lines and `snapshot.js` ~110. Small enough to read before you trust it.

## Benchmarks

`bench/` has a small SPA shaped like a real dashboard — demo login, a list with search and sort, a
detail page, API calls over the network, and an optional long-poll like a realtime fallback.

**With a real agent** — headless Claude Code, built-in tools off, the task given in plain words:

```bash
uv run python bench/agent_race.py --runs 3 --model sonnet
```

| | correct | turns | tokens read | tokens written | cost | time, no long-poll | time, long-poll open |
|---|---|---|---|---|---|---|---|
| **pointclick** | 6/6 | **7** | **104k** | **620–660** | **$0.056** | **13.7 s** | 16.7 s |
| Playwright MCP 0.0.81 | 6/6 | 13 | 266–289k | 1.1k | $0.113–0.118 | 23.3 s | 28.5 s |
| agent-browser 0.38.1 | 6/6 | 12 | 525k | 760–780 | $0.223 | 19.1 s | **14.8 s** |

Medians of 3 runs per column. Turns and tokens barely move between runs; time moves by up to a
third with model latency, so read it as a range. Playwright MCP's action replies link to a
snapshot file instead of including the page, so the model asks for a snapshot after each step.
agent-browser's actions return nothing, so every step is an action plus a snapshot too.

**Tools alone** — the same flow driven by a script, so no model time is included:

```bash
uv run python bench/race.py --runs 8 --poll 0
```

| | calls | text to the model | tool time | with a long-poll open |
|---|---|---|---|---|
| **pointclick** | **6** | **6.6k chars (~1.9k tokens)** | 2.25 s | **2.31 s** |
| Playwright MCP 0.0.81 | 6 | 17.0k chars (~4.9k tokens) | 3.32 s | 8.27 s |
| agent-browser 0.38.1 | 18 | 12.3k chars (~3.5k tokens) | **1.13 s** | 1.12 s |

agent-browser's tool time is lowest because its actions return nothing: it re-snapshots, and the
script polls for free. In a real agent every one of those calls is a model turn.

**Tool definitions**, sent with every request by clients that don't load tools lazily (all four
estimated the same way, JSON characters ÷ 3.5):

| | tools | definitions |
|---|---|---|
| **pointclick** | **8** | **~0.8k tokens** |
| Playwright MCP | 26 | ~5.2k tokens |
| Chrome DevTools MCP | 29 | ~6.9k tokens |
| agent-browser (`core`) | 29 | ~18.4k tokens |

Measured on an M-series Mac with Chrome 153. Raw results are in `bench/*.json`.

## When to use something else

- **Playwright MCP** — you need network interception, tracing, PDF export, or the full Playwright
  surface.
- **Chrome DevTools MCP** — performance traces, Lighthouse-style audits, deep DevTools debugging.
- **agent-browser** — you'd rather drive the browser from a shell (CLI + skills), want saved auth
  profiles, or need its long tail of commands.
- **Stagehand** — you want natural-language `act`/`extract` backed by its own model, or hosted
  browsers on Browserbase.

pointclick does one job: get an agent through a web app with as little text and as few turns as
it can — locally, with no account and no second model.

## Security

- Everything on a page — text, labels, titles — is **untrusted input** to your model. A page can
  try to instruct your agent. Keep the agent's permissions narrow and confirm consequential steps.
- Each session is an isolated browser context: no access to your Chrome profile, cookies or
  passwords. Nothing persists after `close()`.
- `evaluate` runs arbitrary JavaScript in the page; `upload` reads files you name from disk.

## Limits

- The list is viewport-only by default; long pages take `SCROLL_DOWN` or `observe(full=true)`.
- Every action returns the whole list, not a diff.
- Chromium only, one page at a time per server. Calls sent in parallel run one after another.
- No network inspection, PDF export or dialog control; `evaluate` covers some of it.

## Development

```bash
uv sync --group dev
uv run pytest     # 45 tests; fixture pages served from two local origins
uv run ruff check . && uv run ruff format --check .
```

To release, bump `version` in `pyproject.toml`, then push a matching tag (`v0.1.0`). `release.yml` publishes
to PyPI through trusted publishing; there is no token to keep.

## Credits

The page snapshot is adapted from [jev-ultrafast](https://github.com/browser-use/jev-ultrafast),
MIT © 2026 Browser Use — see `LICENSE-jev`.

## License

MIT
