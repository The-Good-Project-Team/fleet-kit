#!/usr/bin/env python3
"""test_journey_walker.py -- gh#657 acceptance criteria.

WHAT IS AND ISN'T PROVEN HERE. This sandbox has no path to the real philanthropy.org (it
403s -- Cloudflare, the exact BLOCKED condition sentry.md's own mandate describes) and no test
credentials, so this file cannot demonstrate the walker against the real product. What it CAN
prove, and does, against a tiny local fixture server plus the real installed Playwright/chromium
(no mocking of the browser itself):

  * LiveFixtureMutationGateTest.test_sign_in_journey_passes_against_working_fixture -- the
    `sign-in` journey implementation, run for real, correctly detects a working sign-in flow
    end to end (all 3 steps, matching journeys.yaml's own step count) and writes real
    screenshots.
  * LiveFixtureMutationGateTest.test_sign_in_journey_fails_correctly_without_crashing -- the
    AC5 mutation gate: the SAME code, pointed at a fixture whose submit handler was
    deliberately broken (no redirect away from /login), correctly records step 0 as pass and
    step 1 as fail with a captured detail and screenshot -- and does not raise. The journey
    function's own early-return means step 2 is never attempted once step 1's precondition
    (being signed in) did not hold, which is itself the correct behavior: a person would not
    keep clicking through nav checks after a broken sign-in either.

Everything else (RunAllContinuesTest, BlockedJourneyTest, CatalogCoverageTest) is pure-Python
logic tested without a browser, same split journey_issue_filer's own tests use between pure
builders and executed calls.
"""
from __future__ import annotations

import http.server
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_walker as jw  # noqa: E402
import journey_issue_filer as jif  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "members" / "sentry" / "journeys.yaml"

LOGIN_HTML = """<!doctype html><html><body>
<form id="f">
  <label>Email <input type="email" id="email"></label>
  <label>Password <input type="password" id="password"></label>
  <button type="submit">Sign in</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', function (e) {{
  e.preventDefault();
  {action}
}});
</script>
</body></html>
"""

ACCOUNT_HTML = """<!doctype html><html><body>
<nav>Alice <button>Sign out</button></nav>
</body></html>
"""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FixtureServer:
    """A minimal static file server for one login/account fixture, on its own port."""

    def __init__(self, redirects: bool):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        action = "location.href = '/account.html';" if redirects else "/* deliberately broken: no redirect */"
        (root / "login.html").write_text(LOGIN_HTML.format(action=action))
        (root / "account.html").write_text(ACCOUNT_HTML)
        self.port = _free_port()
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **kw)  # noqa: E731
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()


class _HeaderRecordingServer:
    """A minimal HTTP server that just records every request's headers -- used to assert the
    bypass header attaches (or doesn't) against a REAL recorded request server-side, per gh#729
    AC1's own wording ("asserted against a recorded/mock request, not by eyeballing prod").
    Deliberately not `route.continue_`'s own Request object: that call dispatches a fresh
    request Playwright's `request` event does not reliably reflect back with the override
    applied, so the only trustworthy witness is what actually arrived on the wire."""

    def __init__(self):
        self.received: list[dict] = []
        received = self.received

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):  # quiet -- keep test output readable
                pass

        self.port = _free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class CatalogCoverageTest(unittest.TestCase):
    """Every journey in the real catalog has a real implementation -- AC1."""

    def test_catalog_loads(self):
        catalog = jw.load_catalog(CATALOG_PATH)
        self.assertEqual(len(catalog["journeys"]), 10)

    def test_every_catalog_id_has_a_runner(self):
        catalog = jw.load_catalog(CATALOG_PATH)
        ids = {j["id"] for j in catalog["journeys"]}
        self.assertEqual(ids, set(jw.JOURNEY_RUNNERS.keys()))

    def test_step_text_applies_mobile_override(self):
        catalog = jw.load_catalog(CATALOG_PATH)
        sign_in = next(j for j in catalog["journeys"] if j["id"] == "sign-in")
        step2 = sign_in["steps"][2]
        action, observable = jw.step_text(step2, "mobile_390")
        self.assertIn("hamburger", action.lower())
        desktop_action, _ = jw.step_text(step2, "desktop")
        self.assertNotEqual(action, desktop_action)


class BlockedJourneyTest(unittest.TestCase):
    """A journey with missing test-user config is BLOCKED, not filed as a failure -- sentry.md's
    BLOCKED-vs-BROKEN mandate, applied to journeys the same way it already applies to crawls."""

    def test_missing_credentials_raises_blocked_not_a_bare_exception(self):
        users = jw.TestUsers(env={})
        with self.assertRaises(jw.Blocked):
            users.require_users("alice")

    def test_run_all_excludes_blocked_journeys_from_output(self):
        catalog = {
            "viewports": {"desktop": {"width": 1280, "height": 800}},
            "journeys": [{"id": "sign-in", "name": "Sign in", "viewports": ["desktop"],
                          "steps": [{"action": "a", "observable_result": "o"}]}],
        }
        users = jw.TestUsers(env={})  # no ALICE_EMAIL/PASSWORD -> run_sign_in raises Blocked
        journeys_out, blocked = jw.run_all(catalog, users, browser=None, base_out=Path("/tmp"), run_id="r1")
        self.assertEqual(journeys_out, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["id"], "sign-in")


class RedactSecretTest(unittest.TestCase):
    """gh#729 AC6: the bypass value must never survive into a step's `detail` text, a Blocked
    reason, or anything printed -- proven against a fixture string containing the value, same
    shape selftest.py's own `_redact_secrets` tests use for its own secrets."""

    def test_redacts_the_configured_value(self):
        text = "TimeoutError: waiting for selector (sent X-Atlas-Test-Bypass: sekrit-val-123)"
        redacted = jw.redact_secret(text, "sekrit-val-123")
        self.assertNotIn("sekrit-val-123", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_noop_when_no_secret_configured(self):
        text = "some ordinary failure text"
        self.assertEqual(jw.redact_secret(text, None), text)
        self.assertEqual(jw.redact_secret(text, ""), text)

    def test_noop_when_value_absent_from_text(self):
        text = "some ordinary failure text"
        self.assertEqual(jw.redact_secret(text, "sekrit-val-123"), text)


class RunAllContinuesTest(unittest.TestCase):
    """AC3 at the orchestration level: one journey's implementation raising an unexpected,
    non-Blocked exception must not stop the remaining journeys in the same pass."""

    def test_unexpected_exception_in_one_journey_does_not_stop_the_rest(self):
        catalog = {
            "viewports": {"desktop": {"width": 1280, "height": 800}},
            "journeys": [
                {"id": "boom", "name": "Boom", "viewports": ["desktop"],
                 "steps": [{"action": "a", "observable_result": "o"}]},
                {"id": "fine", "name": "Fine", "viewports": ["desktop"],
                 "steps": [{"action": "a", "observable_result": "o"}]},
            ],
        }

        def boom_runner(ctx):
            raise RuntimeError("simulated walker bug")

        def fine_runner(ctx):
            ctx.step(0, lambda: None)

        original = dict(jw.JOURNEY_RUNNERS)
        jw.JOURNEY_RUNNERS["boom"] = boom_runner
        jw.JOURNEY_RUNNERS["fine"] = fine_runner
        self.addCleanup(lambda: jw.JOURNEY_RUNNERS.clear() or jw.JOURNEY_RUNNERS.update(original))

        journeys_out, blocked = jw.run_all(catalog, jw.TestUsers(env={}), browser=None,
                                            base_out=Path("/tmp"), run_id="r1")
        by_id = {j["id"]: j for j in journeys_out}
        self.assertIn("boom", by_id)
        self.assertEqual(by_id["boom"]["steps"][0]["status"], "fail")
        self.assertIn("fine", by_id)
        self.assertEqual(by_id["fine"]["steps"][0]["status"], "pass")


class LiveFixtureMutationGateTest(unittest.TestCase):
    """AC5: a deliberately broken journey fails correctly. Real Playwright, real chromium,
    real HTTP server -- no mocking of the browser."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.good = _FixtureServer(redirects=True)
        cls.broken = _FixtureServer(redirects=False)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        cls.tmp = tempfile.TemporaryDirectory()
        catalog = jw.load_catalog(CATALOG_PATH)
        cls.sign_in_journey = next(j for j in catalog["journeys"] if j["id"] == "sign-in")
        cls.desktop_dims = catalog["viewports"]["desktop"]

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.good.stop()
        cls.broken.stop()
        cls.tmp.cleanup()

    def _ctx(self, base_url: str, run_id: str) -> jw.JourneyCtx:
        users = jw.TestUsers(env={
            "ALICE_EMAIL": "alice@example.com",
            "ALICE_PASSWORD": "hunter2",
            "PHILANTHROPY_BASE_URL": base_url,
        })
        return jw.JourneyCtx(self.sign_in_journey, "desktop", self.desktop_dims, self.browser,
                              users, Path(self.tmp.name), run_id)

    def test_sign_in_journey_passes_against_working_fixture(self):
        run_id = "pass-run"
        ctx = self._ctx(self.good.base_url, run_id)
        jw.run_sign_in(_PatchedCtx(ctx, self.good.base_url))
        statuses = [s["status"] for s in ctx.results]
        self.assertEqual(statuses, ["pass", "pass", "pass"])
        self.assertEqual(len(ctx.results), 3)
        for index, step in enumerate(ctx.results):
            self.assertIn("screenshot", step)
            shot = Path(self.tmp.name) / run_id / "journeys" / "sign-in" / "desktop" / f"{index}.png"
            self.assertTrue(shot.exists(), f"missing screenshot {shot}")
        ctx.close()

    def test_sign_in_journey_fails_correctly_without_crashing(self):
        ctx = self._ctx(self.broken.base_url, "fail-run")
        jw.run_sign_in(_PatchedCtx(ctx, self.broken.base_url))
        statuses = [s["status"] for s in ctx.results]
        # step 0 (form renders) still passes -- the mutation only broke the submit's redirect.
        # step 1 (fill + submit + expect navigation) fails, with a captured detail.
        # step 2 (nav check) is never attempted: the journey function returns as soon as step 1
        # fails, since a person would not proceed past a sign-in that visibly didn't work.
        self.assertEqual(statuses, ["pass", "fail"])
        self.assertIn("detail", ctx.results[1])
        self.assertTrue(ctx.results[1]["detail"])
        ctx.close()


class _PatchedCtx:
    """Wraps a JourneyCtx so `page()` navigates against the fixture's own /login.html and
    /account.html instead of journeys.yaml's literal https://philanthropy.org paths -- the
    static file server used here has no path-based routing, unlike the real product. Delegates
    everything else (step recording, screenshots) to the real JourneyCtx unchanged, so the
    mutation-gate assertions above exercise the SAME step()/close() code the walker ships with.
    """

    def __init__(self, ctx: jw.JourneyCtx, fixture_base: str):
        self._ctx = ctx
        self._fixture_base = fixture_base
        # monkeypatch users.url() for the lifetime of this journey run only
        real_url = ctx.users.url

        def patched(literal_url: str) -> str:
            resolved = real_url(literal_url)
            if resolved.rstrip("/").endswith("/login"):
                return fixture_base + "/login.html"
            if resolved.rstrip("/").endswith("/account"):
                return fixture_base + "/account.html"
            return resolved

        ctx.users.url = patched

    def __getattr__(self, name):
        return getattr(self._ctx, name)


class BypassHeaderScopeTest(unittest.TestCase):
    """gh#729 AC1/AC2/AC3: the bypass header attaches to the configured philanthropy host,
    never attaches to a different host in the same browser context (the fleet-console journey
    shares JourneyCtx.page()'s context-creation code path with every other journey, so this is
    exactly the credential-leak shape a hardcoded per-context header would have had), and an
    unconfigured instance sends no header and still completes normally."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.target = _HeaderRecordingServer()  # stands in for philanthropy.org
        cls.other = _HeaderRecordingServer()   # stands in for an unrelated host (fleet console)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.target.stop()
        cls.other.stop()

    def _ctx(self, bypass: str | None) -> jw.JourneyCtx:
        env = {"PHILANTHROPY_BASE_URL": self.target.base_url}
        if bypass:
            env["ATLAS_TEST_BYPASS"] = bypass
        users = jw.TestUsers(env=env)
        journey = {"id": "probe", "name": "Probe", "steps": []}
        return jw.JourneyCtx(journey, "desktop", {"width": 1280, "height": 800}, self.browser,
                              users, Path("/tmp"), "probe-run")

    def test_bypass_header_attaches_to_the_configured_host(self):
        ctx = self._ctx("sekrit-val-123")
        ctx.page().goto(self.target.base_url + "/", timeout=10000)
        ctx.close()
        self.assertTrue(self.target.received)
        self.assertEqual(self.target.received[-1].get(jw.BYPASS_HEADER.lower()), "sekrit-val-123")

    def test_bypass_header_does_not_attach_to_a_different_host(self):
        ctx = self._ctx("sekrit-val-123")
        ctx.page().goto(self.other.base_url + "/", timeout=10000)
        ctx.close()
        self.assertTrue(self.other.received)
        self.assertNotIn(jw.BYPASS_HEADER.lower(), self.other.received[-1])

    def test_unset_bypass_sends_no_header_and_completes_normally(self):
        ctx = self._ctx(None)
        response = ctx.page().goto(self.target.base_url + "/", timeout=10000)
        ctx.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.target.received)
        self.assertNotIn(jw.BYPASS_HEADER.lower(), self.target.received[-1])


class ReportPage403BlockedTest(unittest.TestCase):
    """gh#729 AC4: a report-page request that comes back 403 -- even with the bypass header
    sent -- is classified BLOCKED (not BROKEN, not a silent pass), and the record carries the
    response status and headers."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        class _ChallengeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.send_header("X-Test-Marker", "cf-challenge")
                self.end_headers()
                self.wfile.write(b"<html><body>Checking your browser...</body></html>")

            def log_message(self, *a):  # quiet -- keep test output readable
                pass

        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), _ChallengeHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_403_on_report_page_is_blocked_with_status_and_headers_recorded(self):
        catalog = {
            "viewports": {"desktop": {"width": 1280, "height": 800}},
            "journeys": [{
                "id": "open-990-report", "name": "Open 990 report", "viewports": ["desktop"],
                "steps": [{"action": "a", "observable_result": "o"}, {"action": "b", "observable_result": "o"}],
            }],
        }
        users = jw.TestUsers(env={
            "PHILANTHROPY_BASE_URL": f"http://127.0.0.1:{self.port}",
            "FIXTURE_EIN": "123456789",
            "ATLAS_TEST_BYPASS": "sekrit-val-123",
        })
        journeys_out, blocked = jw.run_all(catalog, users, self.browser, Path("/tmp"), "blocked-run")

        self.assertEqual(journeys_out, [])  # not a silent pass, and never filed as BROKEN
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["id"], "open-990-report")
        self.assertEqual(blocked[0]["response"]["status"], 403)
        headers = {k.lower(): v for k, v in blocked[0]["response"]["headers"].items()}
        self.assertEqual(headers.get("x-test-marker"), "cf-challenge")
        self.assertNotIn("sekrit-val-123", json.dumps(blocked))  # AC6, defense in depth


class WalkerOutputFeedsIssueFilerTest(unittest.TestCase):
    """The other half of the seam: gh#660's journey_issue_filer.py already ships expecting
    exactly this file's results.json shape. Runs the real walker against the broken fixture,
    then feeds its real output straight into journey_issue_filer.process() (dry-run, no `gh`
    calls) to prove the two independently-built modules actually fit together end to end."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.broken = _FixtureServer(redirects=False)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.broken.stop()
        cls.tmp.cleanup()

    def test_a_failed_step_from_a_real_walker_run_is_filed_by_the_real_filer(self):
        catalog = jw.load_catalog(CATALOG_PATH)
        sign_in = next(j for j in catalog["journeys"] if j["id"] == "sign-in")
        sign_in["viewports"] = ["desktop"]  # one viewport is enough to prove the seam
        users = jw.TestUsers(env={
            "ALICE_EMAIL": "alice@example.com",
            "ALICE_PASSWORD": "hunter2",
            "PHILANTHROPY_BASE_URL": self.broken.base_url,
        })
        real_url = users.url
        users.url = lambda literal: (
            self.broken.base_url + "/login.html" if real_url(literal).rstrip("/").endswith("/login")
            else self.broken.base_url + "/account.html" if real_url(literal).rstrip("/").endswith("/account")
            else real_url(literal)
        )

        journeys_out, blocked = jw.run_all(catalog, users, self.browser, Path(self.tmp.name),
                                            "filer-integration-run", journey_filter=["sign-in"])
        self.assertEqual(blocked, [])
        results = {"run": "filer-integration-run", "deploy_sha": "deadbeef", "journeys": journeys_out}
        results_path = Path(self.tmp.name) / "results.json"
        results_path.write_text(json.dumps(results))

        summary = jif.process(results_path, Path(self.tmp.name) / "state.json", dry_run=True)
        self.assertEqual(len(summary["filed"]), 1, summary)
        self.assertEqual(summary["filed"][0]["key"], jif.step_key("sign-in", 1))


if __name__ == "__main__":
    unittest.main()
