#!/usr/bin/env python3
"""test_journey_walker_wait_until.py -- gh#956 acceptance criteria, run without a real browser.

journey_walker.py's real fix here is proven against live Playwright/chromium in
test_journey_walker.py (a fixture page with a slow subresource), but that file needs a real
browser and is NOT wired into CI (playwright-tests, the job that installs chromium, is
explicitly not the required check -- see ci.yml's own comment on gh#804: a required check must
never run a step that reaches a third party). AC4 requires the required `selftest` job to run a
named test exercising criterion 1, so this file proves the same defect and fix at the unit
level instead: journey_walker must call page.goto() with wait_until="domcontentloaded" by
default (not Playwright's own "load" default, which blocks on every subresource), with a
per-step override available and logged. A FakePage stands in for a real browser page and
simulates exactly the failure mode gh#956 describes -- goto() raises a TimeoutError when asked
to wait_until="load" against a page with a subresource that never finishes loading, and returns
cleanly under "domcontentloaded" -- so this test fails if journey_walker's default ever
regresses back to "load".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import journey_walker as jw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "members" / "sentry" / "journeys.yaml"


class HangingSubresourceTimeoutError(Exception):
    """Stands in for playwright.sync_api.TimeoutError -- this file must not import playwright
    (it must run with no browser installed, same constraint as every other required-check
    test), so it names its own exception rather than depending on that package existing."""


class FakePage:
    """A page whose HTML/asserted content is present immediately, but which also carries a
    subresource that never finishes loading -- exactly the PRD's criterion 1 fixture. Under
    Playwright's real "load" semantics that subresource would block goto() until its own
    timeout; this fake reproduces that observable behavior directly rather than needing a real
    hung TCP connection to prove the same branch."""

    def __init__(self):
        self.calls: list[dict] = []

    def goto(self, url, timeout=None, wait_until=None):
        self.calls.append({"url": url, "timeout": timeout, "wait_until": wait_until})
        if wait_until == "load":
            raise HangingSubresourceTimeoutError(
                f"Page.goto: Timeout {timeout}ms exceeded ... waiting until \"load\""
            )
        return f"response-for-{url}"


def _fake_ctx(steps, viewport="desktop"):
    journey = {"id": "fixture-journey", "steps": steps}
    return jw.JourneyCtx(journey, viewport, dims={}, browser=None, users=None,
                          base_out=Path("/tmp"), run_id="test")


class DefaultWaitUntilAvoidsHangingSubresourceTest(unittest.TestCase):
    """AC1: a step with no catalog override must not false-fail on a hanging subresource."""

    def test_step_with_no_override_passes_against_hanging_subresource_fixture(self):
        ctx = _fake_ctx([{"action": "a", "observable_result": "o"}])
        page = FakePage()
        response = ctx.goto(0, page, "https://fixture.example/hangs")
        self.assertEqual(response, "response-for-https://fixture.example/hangs")
        self.assertEqual(page.calls[0]["wait_until"], "domcontentloaded")

    def test_same_fixture_would_have_false_failed_under_playwrights_own_default(self):
        """Documents the regression this fix closes: wait_until="load" (Playwright's own
        default, and this codebase's behavior before gh#956) DOES raise against the exact
        same fixture -- proving the fixture is a faithful stand-in for the real bug, not a
        fake that would pass regardless of which wait_until was used."""
        page = FakePage()
        with self.assertRaises(HangingSubresourceTimeoutError):
            page.goto("https://fixture.example/hangs", timeout=15000, wait_until="load")


class PerStepOverrideTest(unittest.TestCase):
    """AC3: a step MAY override the default, and the walker applies the override, not the
    default, when one is present."""

    def test_explicit_step_override_is_applied(self):
        ctx = _fake_ctx([{"action": "a", "observable_result": "o", "wait_until": "load"}])
        page = FakePage()
        with self.assertRaises(HangingSubresourceTimeoutError):
            ctx.goto(0, page, "https://fixture.example/hangs")
        self.assertEqual(page.calls[0]["wait_until"], "load")

    def test_viewport_override_wins_over_step_level_override(self):
        steps = [{
            "action": "a", "observable_result": "o", "wait_until": "load",
            "viewport_overrides": {"mobile_390": {"wait_until": "domcontentloaded"}},
        }]
        ctx = _fake_ctx(steps, viewport="mobile_390")
        page = FakePage()
        ctx.goto(0, page, "https://fixture.example/hangs")  # must not raise
        self.assertEqual(page.calls[0]["wait_until"], "domcontentloaded")

    def test_precondition_navigation_with_no_step_index_uses_default(self):
        ctx = _fake_ctx([{"action": "a", "observable_result": "o", "wait_until": "load"}])
        page = FakePage()
        ctx.goto(None, page, "https://fixture.example/login")  # index=None: not a catalog step
        self.assertEqual(page.calls[0]["wait_until"], "domcontentloaded")


class StepWaitUntilResolutionTest(unittest.TestCase):
    """Pure logic, no browser at all -- step_wait_until is the single source of truth ctx.goto
    defers to, so it gets its own direct coverage of the resolution order."""

    def test_defaults_when_step_sets_nothing(self):
        self.assertEqual(jw.step_wait_until({}, "desktop"), jw.DEFAULT_WAIT_UNTIL)
        self.assertEqual(jw.DEFAULT_WAIT_UNTIL, "domcontentloaded")

    def test_step_level_override(self):
        self.assertEqual(jw.step_wait_until({"wait_until": "networkidle"}, "desktop"), "networkidle")

    def test_viewport_override_beats_step_level(self):
        step = {"wait_until": "load", "viewport_overrides": {"mobile_390": {"wait_until": "networkidle"}}}
        self.assertEqual(jw.step_wait_until(step, "mobile_390"), "networkidle")
        self.assertEqual(jw.step_wait_until(step, "desktop"), "load")


class CatalogEnumerationTest(unittest.TestCase):
    """The PRD's own open question (UNKNOWN #1): does any of the 10 catalog journeys rely on
    "load" semantics today? Answer, enumerated here rather than only in a PR description: no --
    every step in the real catalog resolves to DEFAULT_WAIT_UNTIL. This is a live check, not a
    one-time claim: it fails the moment a future step adds a wait_until override, which is
    exactly the point where a human should notice and review it."""

    def test_no_catalog_step_currently_overrides_wait_until(self):
        catalog = jw.load_catalog(CATALOG_PATH)
        overriding = []
        for journey in catalog["journeys"]:
            for i, step in enumerate(journey["steps"]):
                for viewport in journey.get("viewports") or ["desktop"]:
                    resolved = jw.step_wait_until(step, viewport)
                    if resolved != jw.DEFAULT_WAIT_UNTIL:
                        overriding.append((journey["id"], i, viewport, resolved))
        self.assertEqual(overriding, [], f"unexpected wait_until overrides: {overriding}")


if __name__ == "__main__":
    unittest.main()
