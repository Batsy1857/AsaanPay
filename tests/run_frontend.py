"""Run dependency-free JS checks and UI smoke in an installed Chrome/Chromium.

Requires a running AsaanPay API; creates a few synthetic attempts, without deleting
existing data. The temporary localhost server exposes only test/static files and
proxies API calls to that service. No browser driver or npm packages are required.
"""

import argparse
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))) if self.command == "POST" else None
            headers = {key: self.headers[key] for key in ("Content-Type", "Idempotency-Key") if key in self.headers}
            request = Request(self.server.api + path, data=body, headers=headers, method=self.command)
            try:
                response = urlopen(request, timeout=15)
            except HTTPError as error:
                response = error
            with response:
                self.send_body(response.status, response.read(), response.headers.get("Content-Type", "application/json"))
            return
        files = {
            "/": "app/static/index.html",
            "/tests/frontend.html": "tests/frontend.html",
            "/tests/frontend_tests.js": "tests/frontend_tests.js",
            "/tests/fixtures/timing_traces.json": "tests/fixtures/timing_traces.json",
            **{"/static/" + name: "app/static/" + name for name in ("app.js", "core.js", "version.js", "styles.css")},
        }
        if self.command != "GET" or path not in files:
            self.send_error(404)
            return
        file = ROOT / files[path]
        self.send_body(200, file.read_bytes(), mimetypes.guess_type(file)[0] or "text/plain")

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:18000")
    parser.add_argument("--chrome", default=shutil.which("chromium") or shutil.which("google-chrome")
                        or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in ("localhost", "127.0.0.1"):
        parser.error("Use a local sandbox API URL.")
    if not Path(args.chrome).is_file():
        parser.error("Chrome/Chromium was not found; pass --chrome /path/to/browser.")
    with urlopen(args.url.rstrip("/") + "/api/health", timeout=5) as response:
        if response.status != 200:
            parser.error("The sandbox API must be healthy.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.api = args.url.rstrip("/")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="asaanpay-chrome-") as profile:
            command = [args.chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                       "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-extensions",
                       "--user-data-dir=" + profile, "--window-size=1440,1100", "--dump-dom", "--virtual-time-budget=30000",
                       f"http://127.0.0.1:{server.server_port}/tests/frontend.html"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            match = re.search(r'<pre id="test-results">(.*?)</pre>', result.stdout, re.S)
            print(unescape(match.group(1)) if match else "No test report returned by Chrome.")
            passed = result.returncode == 0 and "<title>ASAANPAY_TESTS_PASSED</title>" in result.stdout
            if not passed:
                print(result.stderr[-2000:])
            return 0 if passed else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
