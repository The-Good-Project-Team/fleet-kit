#!/usr/bin/env python3
"""Regression test for gh#553 VP review round 2, fix 1.

Both consoles (fleet_home.html and fleet_view.html/classic) are served in production behind a
reverse proxy under a path prefix (e.g. https://dino.luckymachines.co/fleet/philanthropy/), but
neither page's server has any idea that prefix exists. Before this fix, every fetch() call and
every link (href="/classic", href="/status", getJSON('/api/...')) was written root-absolute, so
it reached the proxy's own domain root instead of this instance -- and the root /api/* endpoint
picked a backend by sniffing the Referer header, silently serving one instance's numbers under
another instance's brand whenever a browser stripped that header (privacy mode, an in-app
webview, Referrer-Policy: no-referrer).

This drives each page in a real headless browser (playwright), served at a path prefix, and
asserts that every API request and every link the page renders carries that same prefix --
never one request landing bare at the domain root.

Run: python3 scripts/test_console_path_prefix.py
"""
from __future__ import annotations

import http.server
import threading
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
HOME_HTML = (HERE / "fleet_home.html").read_bytes()
CLASSIC_HTML = (HERE / "fleet_view.html").read_bytes()
PREFIX = "/fleet/demo-instance/"

ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == PREFIX or path == PREFIX.rstrip("/"):
            body = HOME_HTML
        elif path == PREFIX + "classic":
            body = CLASSIC_HTML
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _mock_api(route, requested):
    url = route.request.url
    requested.append(url)
    path = urlparse(url).path
    if path.endswith("/api/stream"):
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body="")
        return
    route.fulfill(json={"runs": [], "gh": {}, "members": [], "asks": [], "kpi": [],
                         "configured": False, "FLEET_ENABLED": True, "next_fires": [],
                         "open": [], "recently_resolved": [], "counts": {}})


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

            # --- Home, served under the prefix ---------------------------------------------
            page = browser.new_page(viewport={"width": 390, "height": 844})
            requested = []
            page.route("**/api/**", lambda route: _mock_api(route, requested))
            page.goto(f"{base}{PREFIX}", timeout=15000)
            page.wait_for_timeout(800)

            def _home_api_calls_carry_the_prefix():
                assert requested, "no /api/* calls observed at all"
                bad = [u for u in requested if urlparse(u).path.startswith("/api/")]
                assert not bad, f"these fetches skipped the prefix and hit the domain root: {bad}"
                good = [u for u in requested if urlparse(u).path.startswith(PREFIX + "api/")]
                assert good, f"no fetch carried the expected prefix either: {requested}"
            check("Home's /api/* fetches all carry the instance's path prefix", _home_api_calls_carry_the_prefix)

            def _home_links_carry_the_prefix():
                stats_href = page.get_attribute("#health a", "href")
                assert stats_href == f"{PREFIX}classic#stats", stats_href
                foot_hrefs = page.eval_on_selector_all("#foot a", "els => els.map(e => e.getAttribute('href'))")
                assert foot_hrefs == [f"{PREFIX}classic", f"{PREFIX}classic#settings", f"{PREFIX}status"], foot_hrefs
            check("Home's classic/status links all carry the instance's path prefix", _home_links_carry_the_prefix)
            page.close()

            # --- Classic, served under the prefix -------------------------------------------
            page2 = browser.new_page(viewport={"width": 1280, "height": 900})
            requested2 = []
            page2.route("**/api/**", lambda route: _mock_api(route, requested2))
            page2.goto(f"{base}{PREFIX}classic", timeout=15000)
            page2.wait_for_timeout(800)

            def _classic_api_calls_carry_the_prefix():
                assert requested2, "no /api/* calls observed at all"
                bad = [u for u in requested2 if urlparse(u).path.startswith("/api/")]
                assert not bad, f"these fetches skipped the prefix and hit the domain root: {bad}"
                good = [u for u in requested2 if urlparse(u).path.startswith(PREFIX + "api/")]
                assert good, f"no fetch carried the expected prefix either: {requested2}"
            check("Classic's /api/* fetches (incl. the live-stream EventSource) all carry the instance's path prefix",
                  _classic_api_calls_carry_the_prefix)
            page2.close()

            browser.close()
    finally:
        server.shutdown()

    for name in ok:
        print(f"ok   {name}")
    for name, err in fail:
        print(f"FAIL {name}\n     {err}")
    print(f"\n{len(ok)} passed, {len(fail)} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
