import functools
import http.server
import shutil
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import Error as PWError
from playwright.async_api import async_playwright

from pointclick import server

PAGES = Path(__file__).parent / "pages"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # /slow?ms=N answers after N ms.
        if self.path.startswith("/slow"):
            time.sleep(int(parse_qs(urlparse(self.path).query)["ms"][0]) / 1000)
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def _serve(host):
    handler = functools.partial(Quiet, directory=str(PAGES))
    httpd = http.server.ThreadingHTTPServer((host, 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture(scope="session")
def origins():
    # 127.0.0.1 and localhost are different sites, so the child iframe is a real out-of-process frame.
    a, b = _serve("127.0.0.1"), _serve("127.0.0.1")
    yield f"http://127.0.0.1:{a.server_port}", f"http://localhost:{b.server_port}"
    a.shutdown()
    b.shutdown()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def browser():
    yield server
    await server.close()


@pytest.fixture
async def running(tmp_path, monkeypatch):
    # A browser someone else started, with its own profile and a tab of its own; pointclick attaches over CDP.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    async with async_playwright() as pw:
        args = [f"--remote-debugging-port={port}"]
        try:
            ctx = await pw.chromium.launch_persistent_context(tmp_path / "profile", channel="chrome", args=args)
        except PWError:
            ctx = await pw.chromium.launch_persistent_context(tmp_path / "profile", args=args)
        monkeypatch.setattr(server, "CDP_URL", f"http://127.0.0.1:{port}")
        yield ctx
        await server.close()
        await ctx.close()


@pytest.fixture
async def chromium_build():
    # Any Chromium on disk that isn't reached through a channel: Playwright's own, or an installed Chrome.
    async with async_playwright() as pw:
        bundled = pw.chromium.executable_path
    for path in (
        bundled,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
    ):
        if path and Path(path).exists():
            return path
    pytest.skip("no Chromium build on disk")
