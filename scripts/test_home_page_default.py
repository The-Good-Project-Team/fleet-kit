#!/usr/bin/env python3
"""Regression test for gh#560.

fleet_view.html used to boot straight onto whichever agent's raw run log the page picked (or,
after gh#228, onto PRs & Backlog) -- either way, a fresh console load never showed the number
this fleet exists to move. This drives the real page in a real headless browser (playwright)
against a mocked /api/snapshot + /api/number and checks that:
  - a clean load (no SELECTED in URL/localStorage) renders the Home page -- the number plus
    three fleet-health tiles (alive, waiting on human, merged last 24h) -- not an agent's log
    (AC1, AC2)
  - clicking an agent in the sidebar still opens that agent's own detail/log view exactly as
    before (AC3, no regression to selectAgent())
  - an instance with no number configured renders an explicit "unavailable" state, never a
    blank/undefined one (AC1's "or an explicit number-unavailable state")

Run: python3 scripts/test_home_page_default.py
"""
from __future__ import annotations

import datetime
import http.server
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE_HTML = (HERE / "fleet_view.html").read_bytes()

ok, fail = [], []


def _iso_hours_ago(hours: float) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


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

    def log_message(self, *a):  # keep test output readable
        pass


RUNS = [
    # newest ok run is datta's, 2 minutes ago (fixed offset -> deterministic "...ago" text).
    {"member": "datta", "status": "ok", "ts": time.time() - 120},
    {"member": "gru", "status": "ok", "ts": time.time() - 9000},
]
MERGED = [
    # within the trailing 24h -- counted.
    {"number": 601, "title": "recent fix", "mergedAt": _iso_hours_ago(2),
     "author": {"login": "minion"}, "files": []},
    # outside the trailing 24h -- NOT counted.
    {"number": 599, "title": "older fix", "mergedAt": _iso_hours_ago(30),
     "author": {"login": "minion"}, "files": []},
]
NEEDS_HUMAN_OP = {"count": 2, "oldest_age_hours": 5.2}
NUMBER_PAYLOAD = {
    "configured": True, "present": True, "stale": False,
    "payload": {
        "as_of": "2026-09-06T18:00:00Z",
        "number": {"name": "Stripe MRR", "value": 1234, "unit": "USD", "delta_7d": 56},
        "guardrail": {"name": "Refund rate", "value": 0.4, "unit": "%", "delta_7d": 0},
        "channel": {"name": "Signups", "value": 12, "unit": "", "delta_7d": 2},
    },
}
MEMBERS = [{"spec": {"name": "datta", "kind": "worker"},
            "effective": {"enabled": True, "max_turns": 40, "model": "x", "schedule": ""},
            "overrides": []}]


def _boot_mocks_factory(number_payload):
    def _boot_mocks(route):
        path = urlparse(route.request.url).path
        if path == "/api/snapshot":
            route.fulfill(json=({"runs": RUNS,
                                  "gh": {"prs": [], "issues": [], "merged": MERGED,
                                         "self_evolution": [], "needs_human_op": NEEDS_HUMAN_OP}}))
        elif path == "/api/number":
            route.fulfill(json=number_payload)
        elif path == "/api/fleet_state":
            route.fulfill(json=({"FLEET_ENABLED": True, "REPO_URL": "", "SIBLINGS": []}))
        elif path == "/api/members":
            route.fulfill(json=({"members": MEMBERS}))
        elif path == "/api/spend":
            route.fulfill(json=({"spend": []}))
        elif path == "/api/kpi":
            route.fulfill(json=({"kpi": []}))
        elif path == "/api/next_fires":
            route.fulfill(json=({"next_fires": []}))
        elif path == "/api/alerts":
            route.fulfill(json=({"open": [], "recently_resolved": [], "counts": {},
                                  "budget_safe": True, "worst": "ok"}))
        elif path == "/api/stream":
            route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body="")
        else:
            route.fulfill(status=404, json=({"error": "unmocked route " + path}))
    return _boot_mocks


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

            # --- Fresh load, number configured -------------------------------------------------
            page = browser.new_page()
            console_errors = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))
            page.route("**/api/**", _boot_mocks_factory(NUMBER_PAYLOAD))
            page.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
                status=200, content_type="application/javascript", body="/* stubbed for test */"))
            page.goto(f"http://127.0.0.1:{port}/", timeout=15000)
            page.wait_for_selector('.side-nav-item[data-page="home"].active', timeout=15000)

            def _no_boot_errors():
                assert not console_errors, f"console/page errors: {console_errors}"
            check("Home page renders with zero console/page errors", _no_boot_errors)

            def _lands_on_home_not_agent():
                title = page.text_content(".detail-head h2")
                assert title == "Console", f"expected the Home page's title, got {title!r}"
                cls = page.get_attribute('.side-nav-item[data-page="home"]', "class") or ""
                assert "active" in cls, f"Home nav item should be active on a fresh load, class={cls!r}"
            check("Fresh load lands on Home, not an agent's log (AC1)", _lands_on_home_not_agent)

            def _renders_the_number():
                body = page.text_content("#runList")
                assert "1,234" in body and "USD" in body, f"number tile missing from: {body[:400]}"
            check("Home page renders the venture's number (AC1)", _renders_the_number)

            def _renders_three_tiles():
                cells = page.eval_on_selector_all(
                    ".runs-kpi-row .kpi-label", "els => els.map(e => e.textContent)")
                assert cells == ["Alive", "Waiting on human", "Merged, last 24h"], cells
                values = page.eval_on_selector_all(
                    ".runs-kpi-row .kpi-value", "els => els.map(e => e.textContent.trim())")
                assert "ago" in values[0], f"alive tile should show an age, got {values[0]!r}"
                assert values[1] == "2", f"waiting-on-human count should be 2, got {values[1]!r}"
                assert values[2] == "1", f"only 1 of 2 merged PRs falls in the trailing 24h, got {values[2]!r}"
            check("Three fleet-health tiles render with correct values (AC2)", _renders_three_tiles)

            def _click_agent_still_opens_its_log():
                page.click('.agent-item[data-agent="datta"]')
                page.wait_for_function("document.querySelector('.detail-head h2').textContent === 'datta'")
                assert page.text_content(".detail-head h2") == "datta"
            check("Clicking a sidebar agent still opens its own log (AC3)", _click_agent_still_opens_its_log)

            page.close()

            # --- Fresh load, no number configured for this instance ----------------------------
            page2 = browser.new_page()
            page2.route("**/api/**", _boot_mocks_factory({"configured": False}))
            page2.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
                status=200, content_type="application/javascript", body="/* stubbed for test */"))
            page2.goto(f"http://127.0.0.1:{port}/", timeout=15000)
            page2.wait_for_selector('.side-nav-item[data-page="home"].active', timeout=15000)

            def _unconfigured_number_is_explicit_not_blank():
                body = page2.text_content("#runList")
                assert "unavailable" in body, f"expected an explicit unavailable state, got: {body[:400]}"
                assert "undefined" not in body and "null" not in body
            check("No number configured renders an explicit state, never blank (AC1)",
                  _unconfigured_number_is_explicit_not_blank)

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
