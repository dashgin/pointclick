"""Serves the bench app. /api/wait?ms=N answers after N ms; /api/longpoll holds for 25 s, like a realtime fallback."""

import functools
import http.server
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/wait"):
            time.sleep(int(parse_qs(urlparse(self.path).query).get("ms", ["0"])[0]) / 1000)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/longpoll"):
            time.sleep(25)
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def serve(port=0):
    handler = functools.partial(Handler, directory=str(Path(__file__).parent))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    s = serve(8765)
    print("http://127.0.0.1:8765/  (add ?poll=1 for the long-poll variant)")
    s.serve_forever()
