"""Regression test for gh#553 fix 1 (and fix 2).

fleet_home.html is the page Reif actually lands on (fk#645), but until this PR it showed only
the venture's number -- percent-of-week, "is the fleet alive", "is anything waiting on me" and
merged-in-the-last-24h all lived one tap away on /classic. This drives the real page in a real
headless browser (playwright) against a mocked backend and checks that the ported health strip
renders correctly, including the case where /api/budget_preview resolves AFTER load()'s own
Promise.all finishes.

That last case is not incidental: `STATE.budget = await getJSON(...)` resolves the STATE object
reference BEFORE awaiting, so if load()'s `STATE = {...STATE, ...}` reassignment (a NEW object,
not a mutation of the old one) completes during that await, the eventual write lands on the
orphaned old object and STATE.budget is silently lost forever. /api/home_summary (gh#876: the
critical-wave request that now backs this health strip, replacing /api/snapshot) is delayed
here specifically to force that ordering.

Run: python3 scripts/test_fleet_home_health.py
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

ok, fail = [], []


def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as exc:  # noqa: BLE001 -- a test file reports, it does not raise
        fail.append((name, f"{type(exc).__name__}: {exc}"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/":
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


RUNS = [{"member": "datta", "status": "ok", "ts": time.time() - 120}]
MERGED = [
    {"number": 601, "title": "recent fix", "mergedAt": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 3600)),
     "author": {"login": "minion"}, "url": "https://github.com/x/y/pull/601"},
]
NEEDS_HUMAN_OP = {"count": 65, "oldest_age_hours": 680.0}


def _mocks(route):
    path = urlparse(route.request.url).path
    if path == "/api/home_summary":
        # Deliberately the slowest response in load()'s critical Promise.all -- forces
        # refreshHealth()'s single-request fetch (never awaited inside that Promise.all) to
        # resolve first, reproducing the stale-STATE-reference race gh#553 fix 1 hit live.
        # gh#876: this route replaced /api/snapshot as the critical wave's health source.
        time.sleep(0.3)
        route.fulfill(json={"newest_ok_run_ts": RUNS[0]["ts"], "merged_24h": 1,
                             "needs_human_op": NEEDS_HUMAN_OP})
    elif path == "/api/snapshot":
        route.fulfill(json={"runs": RUNS, "gh": {"merged": MERGED, "needs_human_op": NEEDS_HUMAN_OP}})
    elif path == "/api/number":
        route.fulfill(json={"configured": False})
    elif path == "/api/plan":
        route.fulfill(json={"objective": "", "checkpoints": "", "source": ""})
    elif path == "/api/asks":
        route.fulfill(json={"asks": []})
    elif path == "/api/members":
        route.fulfill(json={"members": []})
    elif path == "/api/kpi":
        route.fulfill(json={"kpi": []})
    elif path == "/api/spend":
        route.fulfill(json={"spend": []})
    elif path == "/api/build":
        route.fulfill(json={})
    elif path == "/api/fleet_state":
        route.fulfill(json={"FLEET_ENABLED": True, "BRAND": "Philanthropy Atlas"})
    elif path == "/api/budget_preview":
        route.fulfill(json={"week_bank_pct": 42.7})
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
            page.route("**/api/**", _mocks)
            page.goto(f"http://127.0.0.1:{port}/", timeout=15000)
            page.wait_for_timeout(1500)

            def _no_boot_errors():
                assert not errors, f"console/page errors: {errors}"
            check("Home page renders with zero console/page errors", _no_boot_errors)

            def _title_comes_from_brand():
                assert page.title() == "Philanthropy Atlas", page.title()
            check("Tab title comes from this instance's own BRAND config (fix 5)", _title_comes_from_brand)

            def _health_survives_the_late_budget_preview_response():
                # The actual regression: without the fix, this reads "headroom unavailable"
                # forever even though /api/budget_preview answered correctly.
                text = page.text_content("#health")
                assert "42.7% of week" in text, f"percent-of-week missing from: {text!r}"
            check("Percent-of-week survives budget_preview resolving after load() (fix 1)",
                  _health_survives_the_late_budget_preview_response)

            def _health_shows_alive_waiting_merged_and_stats_link():
                text = page.text_content("#health")
                assert "ago" in text, f"alive tile missing from: {text!r}"
                assert "Waiting on human 65" in text, f"waiting-on-human tile missing from: {text!r}"
                assert "oldest 680h" in text, f"oldest age missing from: {text!r}"
                assert "merged 24h 1" in text, f"merged-24h tile missing from: {text!r}"
                href = page.get_attribute("#health a", "href")
                assert href == "/classic#stats", f"Stats link missing or wrong, got {href!r}"
            check("Alive / waiting-on-human / merged-24h tiles and a working Stats link (fix 1)",
                  _health_shows_alive_waiting_merged_and_stats_link)

            def _asks_section_shows_the_needs_human_op_count():
                text = page.text_content("#asksSub")
                assert "65 waiting on human, oldest 680h" in text, text
            check("Needs you shows the needs-human-op count next to the asks (fix 2)",
                  _asks_section_shows_the_needs_human_op_count)

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
