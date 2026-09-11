"""Regression test for gh#553 fix 1, round 2.

fleet_home.html is served behind a path prefix on the real deploy (Caddy routes
/fleet/<instance>/* to this backend -- see scripts/path_health_check.sh), but the page used
to write root-absolute URLs (`/api/...`, `/classic`, `/status`). Those 404 once a browser is
actually sitting under a prefix, and the unprefixed `/api/*` root turned out to be a
*different* route that picks its backend from the Referer header -- so a browser that sends
no Referer would silently show another instance's numbers under this instance's brand.

This drives the real page in a real headless browser (playwright), served at a prefixed path
("/fleet/fleet-kit/") exactly like the real deploy, and checks that every fetch and every
link stays inside that prefix.

Run: python3 scripts/test_fleet_home_base_path.py
"""
from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE_HTML = (HERE / "fleet_home.html").read_bytes()
PREFIX = "/fleet/fleet-kit/"

ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == PREFIX:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE_HTML)))
            self.end_headers()
            self.wfile.write(PAGE_HTML)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


NEEDS_HUMAN_OP = {"count": 19, "oldest_age_hours": 40.0}
REQUEST_URLS = []


def _mocks(route):
    REQUEST_URLS.append(route.request.url)
    path = urlparse(route.request.url).path
    if path == PREFIX + "api/snapshot":
        route.fulfill(json={"runs": [], "gh": {"merged": [], "needs_human_op": NEEDS_HUMAN_OP}})
    elif path == PREFIX + "api/number":
        route.fulfill(json={"configured": False})
    elif path == PREFIX + "api/plan":
        route.fulfill(json={"objective": "", "checkpoints": "", "source": ""})
    elif path == PREFIX + "api/asks":
        route.fulfill(json={"asks": []})
    elif path == PREFIX + "api/members":
        route.fulfill(json={"members": []})
    elif path == PREFIX + "api/kpi":
        route.fulfill(json={"kpi": []})
    elif path == PREFIX + "api/spend":
        route.fulfill(json={"spend": []})
    elif path == PREFIX + "api/build":
        route.fulfill(json={})
    elif path == PREFIX + "api/fleet_state":
        route.fulfill(json={"FLEET_ENABLED": True, "BRAND": "Fleet Kit"})
    elif path == PREFIX + "api/budget_preview":
        route.fulfill(json={"week_bank_pct": 12.3})
    else:
        route.fulfill(status=404, json={"error": "unmocked route " + path})


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            # Deliberately NOT scoped to PREFIX: an unfixed page would fetch the unprefixed
            # /api/* root instead, and this route would still catch it -- which is exactly
            # the bug. The per-request URL assertions below are what actually prove the fix.
            page.route("**/api/**", _mocks)
            page.goto(f"http://127.0.0.1:{port}{PREFIX}", timeout=15000)
            page.wait_for_timeout(1500)

            def _no_boot_errors():
                assert not errors, f"console/page errors: {errors}"
            check("Home page renders with zero console/page errors under a path prefix", _no_boot_errors)

            def _all_api_calls_stayed_inside_the_prefix():
                bad = [u for u in REQUEST_URLS if urlparse(u).path.startswith("/api/")]
                assert not bad, f"fetch(es) escaped the console's own prefix: {bad}"
                assert REQUEST_URLS, "no /api/* calls observed at all"
            check("Every fetch resolves under this console's own path prefix, not domain root",
                  _all_api_calls_stayed_inside_the_prefix)

            def _stats_link_stays_inside_the_prefix():
                href = page.get_attribute("#health a", "href")
                assert href == PREFIX + "classic#stats", f"Stats link escaped the prefix: {href!r}"
            check("Stats link on the FLEET card stays inside the prefix", _stats_link_stays_inside_the_prefix)

            def _footer_links_stay_inside_the_prefix():
                hrefs = page.eval_on_selector_all("#foot a", "els => els.map(e => e.getAttribute('href'))")
                assert hrefs == [PREFIX + "classic", PREFIX + "classic#settings", PREFIX + "status"], hrefs
            check("Footer's Classic/Settings/Status links stay inside the prefix", _footer_links_stay_inside_the_prefix)

            def _health_still_renders_real_data():
                text = page.text_content("#health")
                assert "12.3% of week" in text, text
                assert "Waiting on human 19" in text, text
            check("FLEET card still renders real data once fetches resolve correctly", _health_still_renders_real_data)

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
