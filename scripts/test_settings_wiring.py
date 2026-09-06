#!/usr/bin/env python3
"""Regression test for gh#302.

renderSettingsPage() (fleet_view.html) used to call renderBudgetPreview() one line before its
own `window.renderBudgetPreview = ...` assignment. Referencing that unassigned global threw a
synchronous ReferenceError which aborted the rest of the function -- including the .onclick
wiring for Sign in, stop/start fleet, and Save dials declared further down. Net effect: every
write button on the Settings page fired zero requests, with no error visible anywhere but the
browser console.

This drives the real page in a real headless browser (playwright) and intercepts its fetch
calls, rather than statically checking source order -- it catches any future recurrence of the
same declare-after-use class of bug, not just this exact line. See gh#302's own acceptance
criteria 2-5.

Run: python3 scripts/test_settings_wiring.py
"""
from __future__ import annotations

import http.server
import json
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


# --- a static server for the one file under test; no other route ever gets hit since every
# /api/* call is intercepted client-side by playwright before it leaves the page. ------------
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


def _boot_mocks(route):
    """Canned answers for every endpoint boot() and renderSettingsPage() call, so this test
    exercises real page JS against a fake backend rather than needing a live fleet_view_server
    (gh, subprocess, fleet.env) in CI."""
    path = urlparse(route.request.url).path
    if path == "/api/snapshot":
        route.fulfill(json=({"runs": [], "gh": {"prs": [], "issues": [], "merged": [], "self_evolution": []}}))
    elif path == "/api/fleet_state":
        # FLEET_ENABLED=true so the button reads "stop fleet" and the confirm() branch
        # (acceptance criterion 4) is actually exercised.
        route.fulfill(json=({"FLEET_ENABLED": True, "REPO_URL": "", "SIBLINGS": []}))
    elif path == "/api/members":
        route.fulfill(json=({"members": []}))
    elif path == "/api/spend":
        route.fulfill(json=({"spend": []}))
    elif path == "/api/kpi":
        route.fulfill(json=({"kpi": []}))
    elif path == "/api/next_fires":
        route.fulfill(json=({"next_fires": []}))
    elif path == "/api/budget_preview":
        route.fulfill(json=({"note": "no reading"}))
    elif path == "/api/login":
        route.fulfill(json=({"ok": True}))
    elif path == "/api/fleet_toggle":
        route.fulfill(json=({"ok": True, "state": {"FLEET_ENABLED": False}}))
    elif path == "/api/fleet_settings":
        # gh#480 repro: a bad FLEET_GRU_CADENCE value (e.g. "0,30", a comma list where only a
        # single hour/step/range belongs) is exactly the shape _validate_dial_value rejects
        # server-side with a 400 {"ok": false, "errors": {key: msg}} body. Mirror that one
        # case here so this test can drive the real client code against it; every other body
        # still gets the healthy ok:true response the other checks below rely on.
        body = json.loads(route.request.post_data or "{}")
        if body.get("FLEET_GRU_CADENCE") == "0,30":
            route.fulfill(status=400, json=({
                "ok": False,
                "errors": {"FLEET_GRU_CADENCE": "not a valid cron hour field "
                           "(expected '*', 'N', 'N-M', '*/N', or a comma list, N in 0-23)"},
            }))
        else:
            route.fulfill(json=({"ok": True, "written": ["FLEET_QUEUE_CAP"], "state": {}}))
    elif path == "/api/alerts":
        # gh#399: alert_store.snapshot() shape, healthy case -- worst="ok" so renderAlerts()
        # hides the banner and this test's own "zero console errors" assertion isn't tripped by
        # this route falling through to the unmocked-404 branch below.
        route.fulfill(json=({"open": [], "recently_resolved": [], "counts": {},
                              "budget_safe": True, "worst": "ok"}))
    elif path == "/api/number":
        # gh#513: {"configured": false} is number_read.read_current()'s own shape for an
        # instance with no FLEET_NUMBER_URL -- renderTheNumber() hides the tile on this and
        # never fetches again outside boot()'s own Promise.all, so this one mock is enough
        # (same reasoning as /api/alerts above for the "zero console errors" assertion).
        route.fulfill(json=({"configured": False}))
    elif path == "/api/stream":
        # EventSource: an empty event-stream body it can hang onto quietly. es.onerror doesn't
        # log to console, so this never trips the "zero console errors" assertion below.
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

            requests_seen = []
            page.on("request", lambda r: requests_seen.append((r.method, urlparse(r.url).path)))

            post_bodies = []
            page.on("request", lambda r: post_bodies.append(
                (r.method, urlparse(r.url).path, r.post_data)) if r.post_data else None)

            page.route("**/api/**", _boot_mocks)
            # The page pulls chart.js + its date-fns adapter from a CDN (used by the Stats page,
            # not Settings) -- stub both so this test never depends on live network access in CI.
            page.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
                status=200, content_type="application/javascript", body="/* stubbed for test */"))
            page.goto(f"http://127.0.0.1:{port}/", timeout=15000)

            page.wait_for_selector('.side-nav-item[data-page="settings"]', timeout=15000)

            def _no_boot_errors():
                assert not console_errors, f"console/page errors during boot: {console_errors}"

            check("page boots with zero console/page errors", _no_boot_errors)

            page.click('.side-nav-item[data-page="settings"]')
            page.wait_for_selector("#saveDials", timeout=5000)

            def _settings_renders_clean():
                # acceptance criterion 2: no ReferenceError: renderBudgetPreview is not defined
                assert not console_errors, f"console/page errors on Settings tab: {console_errors}"
                assert ("GET", "/api/budget_preview") in requests_seen, \
                    "renderBudgetPreview() never fired its fetch -- the declare-before-use bug is back"

            check("Settings tab renders with zero console/page errors (gh#302 core bug)", _settings_renders_clean)

            def _sign_in_fires():
                page.fill("#apiKeyInput", "test-key-value")
                page.click("#signIn")
                page.wait_for_function(
                    "document.getElementById('signInMsg').textContent.trim().length > 0", timeout=5000)
                assert ("POST", "/api/login") in requests_seen, "Sign in click never POSTed /api/login"

            check("Sign in click fires /api/login and updates #signInMsg", _sign_in_fires)

            def _stop_fleet_fires():
                page.once("dialog", lambda d: d.accept())
                page.click("#masterToggle")
                page.wait_for_function(
                    "seen => seen.some(([m, p]) => m === 'POST' && p === '/api/fleet_toggle')",
                    arg=requests_seen, timeout=5000)

            check("stop fleet click shows confirm() and fires /api/fleet_toggle", _stop_fleet_fires)

            def _save_dials_fires():
                page.click("#saveDials")
                page.wait_for_function(
                    "document.getElementById('dialsMsg').textContent.trim().length > 0", timeout=5000)
                assert ("POST", "/api/fleet_settings") in requests_seen, \
                    "Save click never POSTed /api/fleet_settings"

            check("Save dials click fires /api/fleet_settings and updates #dialsMsg", _save_dials_fires)

            def _untouched_unset_dial_not_sent():
                # gh#355: /api/fleet_state above returns no dial values at all, so every dial
                # whose option list has no '' entry (FLEET_SHARE_FRACTION,
                # FLEET_GRU_ALLOWANCE_FRACTION, FLEET_DATTA_MAX_NERDS_PER_PASS,
                # FLEET_BUILDER_MODEL, FLEET_CODE_REVIEW_MODEL, FLEET_MAX_BUDGET_USD) rendered
                # with no <option selected> and would previously read back the browser's first-
                # option default. Clicking Save without touching any of them must not persist
                # that fabricated value.
                bodies = [json.loads(pd) for m, p, pd in post_bodies if p == "/api/fleet_settings"]
                assert bodies, "no /api/fleet_settings POST captured"
                last = bodies[-1]
                for hidden_default_key in ("FLEET_SHARE_FRACTION", "FLEET_GRU_ALLOWANCE_FRACTION",
                                            "FLEET_DATTA_MAX_NERDS_PER_PASS", "FLEET_BUILDER_MODEL",
                                            "FLEET_CODE_REVIEW_MODEL", "FLEET_MAX_BUDGET_USD"):
                    assert hidden_default_key not in last, \
                        f"{hidden_default_key} was sent though the operator never touched it: {last}"

            check("Save dials does not send untouched unset dials (gh#355)", _untouched_unset_dial_not_sent)

            def _open_and_dismiss_not_sent():
                # gh#510: PR#501 (gh#460) marked a dial touched on bare `onclick`, so opening
                # FLEET_SHARE_FRACTION's dropdown just to look at the options -- then dismissing
                # it (Escape) without picking anything -- counted as touched and silently saved
                # the browser-default index-0 value ('0.20'). Reproduce that exact sequence
                # (open, then close without moving the selection) rather than
                # page.select_option(), which synthesizes `change`/`input` unconditionally and
                # would never exercise the click-only path this bug lived in.
                sel = '.dial-input[data-key="FLEET_SHARE_FRACTION"]'
                assert page.eval_on_selector(sel, "el => el.value") == "0.20", \
                    "test assumes FLEET_SHARE_FRACTION's unset default renders as index-0 '0.20'"
                page.click(sel)
                page.keyboard.press("Escape")
                assert page.eval_on_selector(sel, "el => el.value") == "0.20", \
                    "selection must not have changed -- this reproduces the open-and-dismiss case, not a real edit"
                with page.expect_response("**/api/fleet_settings"):
                    page.click("#saveDials")
                bodies = [json.loads(pd) for m, p, pd in post_bodies if p == "/api/fleet_settings"]
                last = bodies[-1]
                assert "FLEET_SHARE_FRACTION" not in last, \
                    f"opening and dismissing the dropdown without picking a value must not save it: {last}"

            check("Opening and dismissing a dial's dropdown without picking a value does not save it (gh#510/AC1)",
                  _open_and_dismiss_not_sent)

            def _touched_dial_still_sent():
                # A dial the operator actually picks must still save normally (AC3/AC4).
                page.select_option('.dial-input[data-key="FLEET_BUILDER_MODEL"]', "opus")
                with page.expect_response("**/api/fleet_settings"):
                    page.click("#saveDials")
                bodies = [json.loads(pd) for m, p, pd in post_bodies if p == "/api/fleet_settings"]
                last = bodies[-1]
                assert last.get("FLEET_BUILDER_MODEL") == "opus", \
                    f"operator-picked dial was not sent: {last}"
                assert "FLEET_MAX_BUDGET_USD" not in last, \
                    f"an unrelated untouched unset dial was sent alongside the touched one: {last}"

            check("Touching one dial still saves it without dragging in other unset dials (gh#355)",
                  _touched_dial_still_sent)

            def _validation_error_shows_specific_message():
                # gh#480: resp.errors (plural, a dict) was read as resp.error (singular) and
                # always came back undefined, so every validation failure showed the literal
                # string "save failed" instead of _validate_dial_value's specific reason.
                page.select_option('.dial-input[data-key="FLEET_GRU_CADENCE"]', "__custom__")
                page.fill('.dial-input[data-key="FLEET_GRU_CADENCE"]', "0,30")
                with page.expect_response("**/api/fleet_settings"):
                    page.click("#saveDials")
                page.wait_for_function(
                    "document.getElementById('dialsMsg').textContent.includes('cron hour field')",
                    timeout=5000)
                msg = page.text_content("#dialsMsg")
                assert "FLEET_GRU_CADENCE" in msg and "cron hour field" in msg, \
                    f"expected the server's specific validation message, got: {msg!r}"
                assert msg.strip() != "save failed", "still showing the generic fallback"

            check("A validation failure shows the field's specific error, not 'save failed' (gh#480)",
                  _validation_error_shows_specific_message)

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
