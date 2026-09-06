#!/usr/bin/env python3
"""Regression test for gh#193.

renderQueuePage()'s Backlog section (fleet_view.html) used to render GH.issues in whatever
order they arrived from the server (issue updatedAt recency) with no way to narrow the list --
gh#193's evidence showed 18 fleet:priority-high issues scattered across 36 unsorted rows. This
drives the real page in a real headless browser (playwright) against a mocked /api/snapshot with
a deliberately out-of-priority-order issue list, and checks that:
  - the default render sorts high before medium before low (acceptance: no more scrolling a
    recency-ordered list to find the urgent items)
  - the priority-tier filter buttons narrow the visible rows and update their own counts
  - the title-substring search box narrows the visible rows

Run: python3 scripts/test_queue_filter_wiring.py
"""
from __future__ import annotations

import http.server
import threading
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE_HTML = (HERE / "fleet_view.html").read_bytes()

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

    def log_message(self, *a):  # keep test output readable
        pass


def _issue(number, title, tier_label):
    labels = [{"name": tier_label, "color": "e4a72c"}] if tier_label else []
    return {"number": number, "title": title, "labels": labels, "updatedAt": "2026-09-01T00:00:00Z",
            "_claimed": False}


# Deliberately recency-ordered (arrival order), not priority-ordered -- mirrors gh#193's own
# evidence of high-priority items scattered through the raw GH.issues array.
ISSUES = [
    _issue(190, "medium item at the top", "fleet:priority-medium"),
    _issue(189, "a high priority fix", "fleet:priority-high"),
    _issue(186, "low priority cleanup", "fleet:priority-low"),
    _issue(68, "another high priority fix buried at the bottom", "fleet:priority-high"),
    _issue(5, "unlabeled backlog item", None),
]


def _boot_mocks(route):
    path = urlparse(route.request.url).path
    if path == "/api/snapshot":
        route.fulfill(json=({"runs": [],
                              "gh": {"prs": [], "issues": ISSUES, "merged": [], "self_evolution": []}}))
    elif path == "/api/fleet_state":
        route.fulfill(json=({"FLEET_ENABLED": True, "REPO_URL": "", "SIBLINGS": []}))
    elif path == "/api/members":
        route.fulfill(json=({"members": []}))
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


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()

            console_errors = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))

            page.route("**/api/**", _boot_mocks)
            page.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
                status=200, content_type="application/javascript", body="/* stubbed for test */"))
            page.goto(f"http://127.0.0.1:{port}/", timeout=15000)

            page.wait_for_selector('.side-nav-item[data-page="queue"]', timeout=15000)
            page.click('.side-nav-item[data-page="queue"]')
            page.wait_for_selector("#queueFilterText", timeout=5000)

            def _no_boot_errors():
                assert not console_errors, f"console/page errors: {console_errors}"

            check("Queue page renders with zero console/page errors", _no_boot_errors)

            def _default_sort_is_priority_not_recency():
                nums = page.eval_on_selector_all(
                    ".rail-section:last-child .rail-row a", "els => els.map(e => e.textContent.trim())")
                assert nums == ["#189", "#68", "#190", "#186", "#5"], \
                    f"expected high, high, medium, low, unlabeled order, got {nums}"

            check("Default order is priority tier, high first (gh#193)", _default_sort_is_priority_not_recency)

            def _high_filter_narrows_and_counts():
                page.click('#queueTierFilter button[data-tier="0"]')
                nums = page.eval_on_selector_all(
                    ".rail-section:last-child .rail-row a", "els => els.map(e => e.textContent.trim())")
                assert nums == ["#189", "#68"], f"high filter should show only the two high items, got {nums}"

            check("Priority-tier filter button narrows the backlog to that tier", _high_filter_narrows_and_counts)

            def _all_filter_restores():
                page.click('#queueTierFilter button[data-tier="all"]')
                nums = page.eval_on_selector_all(
                    ".rail-section:last-child .rail-row a", "els => els.map(e => e.textContent.trim())")
                assert len(nums) == 5, f"expected all 5 backlog rows back, got {nums}"

            check("'all' filter button restores every backlog row", _all_filter_restores)

            def _text_search_narrows():
                page.fill("#queueFilterText", "buried")
                page.wait_for_function(
                    "document.querySelectorAll('.rail-section:last-child .rail-row').length === 1")
                nums = page.eval_on_selector_all(
                    ".rail-section:last-child .rail-row a", "els => els.map(e => e.textContent.trim())")
                assert nums == ["#68"], f"title search for 'buried' should isolate #68, got {nums}"

            check("Title-substring search narrows the backlog", _text_search_narrows)

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
