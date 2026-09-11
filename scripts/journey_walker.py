#!/usr/bin/env python3
"""journey_walker -- runs members/sentry/journeys.yaml's catalog end to end via Playwright,
as the existing test users, and writes qa-out/<run>/journeys/results.json in the schema
scripts/journey_issue_filer.py already consumes (gh#657, seam 2 of 7 in #636's decomposition;
#656 gave the catalog, #660's filer already exists downstream of this file's contract).

WHAT THIS DOES NOT KNOW. This module lives in fleet-kit, not in philanthropy.org's own repo,
and this sandbox has no path to the live site (it 403s here -- Cloudflare, per sentry.md's own
BLOCKED-vs-BROKEN mandate) and no test credentials. So every journey below is written against
journeys.yaml's own wording using Playwright's SEMANTIC locators (get_by_label/get_by_role/
get_by_text, matched by the same words the catalog uses for a human) rather than the product's
real CSS/DOM, which this repo cannot see. That is a deliberate choice, not an oversight: a
generic, label-driven locator is far more likely to survive contact with the real product's
markup than a guessed CSS selector would be, and it keeps the mapping from catalog English to
code reviewable line-for-line. It is demonstrated end to end (pass, then a deliberately broken
selector correctly failing without crashing the run) against a small local fixture server in
test_journey_walker.py -- see that file's module docstring for exactly what was and wasn't
proven this way.

CONFIG (env vars; a journey whose required config is missing is BLOCKED, not failed -- see
sentry.md's own "distinguish BLOCKED from BROKEN" mandate. A blocked journey means the CHECKER
lacks a credential, not that the product broke):
  PHILANTHROPY_BASE_URL   default https://philanthropy.org. journeys.yaml's own step actions
                          spell out full https://philanthropy.org/... URLs; this walker swaps
                          in the configured host so it can also be pointed at a staging host or
                          a local fixture (see the test file) without editing the catalog.
  ATLAS_TEST_BYPASS       if set, sent as the `X-Atlas-Test-Bypass` request header on every
                          context this walker opens. The exact header name is this walker's own
                          assumption (the #4507 audit precedent this issue cites lives in the
                          product repo, not here, so the real mechanism was not visible to
                          build against) -- change BYPASS_HEADER in one place if it differs.
  ALICE_EMAIL / ALICE_PASSWORD, BOB_EMAIL / BOB_PASSWORD  -- test user credentials.
  FIXTURE_EIN             a known EIN with a filed 990, for open-990-report and the claim flow.
  FIXTURE_CLAIMED_ORG_URL path (relative to PHILANTHROPY_BASE_URL) of an org admin/settings
                          page alice can administer, for the Verified Org checkout journey.
  NOTIFICATION_DEEPLINK_URL  a thread deep-link URL for bob, for the "open from notification"
                          journey -- this walker has no mailbox/notification-fetch of its own.
  FLEET_CONSOLE_URL       default https://dino.luckymachines.co/fleet/<instance> (<instance>
                          from plan_rank.resolve_instance(), gh#724 -- the bare host is dino's
                          own multi-instance container list, not a fleet console) for the
                          fleet-console journey.

RESULTS.JSON ID CONVENTION (not specified by #656/#660, decided here): a journey run at the
desktop viewport keeps the catalog's own `id` unchanged; a journey run at any OTHER viewport
gets `<id>--<viewport>` as its results.json id, so scripts/journey_issue_filer.py's dedupe key
(`<id>::step<N>`) does not collide a mobile-only failure with a desktop-only one in the same
run. Screenshot directories still use the catalog's bare id (journeys.yaml requires this id
never be renamed) -- only the results.json grouping id carries the suffix.

SEQUENTIAL, NOT PARALLEL (the PRD's own UNKNOWN #1): journeys run one at a time, and each
journey's two viewports run one at a time, inside a single browser instance. Ten journeys times
two viewports is 20 short runs; sequential keeps this walker inside sentry's 900s pass timeout
without opening enough concurrent browser contexts to make a WAF-fronted site's rate limiting
part of the result. If a future pass needs to shorten wall-clock time, parallelizing across
journeys (they don't share state, apart from the two message journeys which already run
alice/bob concurrently within one journey) is the safe axis -- not the two viewports of one
journey, which intentionally never run in relative timing against each other.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_rank  # noqa: E402 -- shared instance-name resolver, same pattern messenger_brief.py uses

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BYPASS_HEADER = "X-Atlas-Test-Bypass"


def redact_secret(text: str, secret: str | None) -> str:
    """Strips a literal secret value out of free text before it is stored in a step's
    `detail`, a Blocked reason, or printed to stderr -- gh#729 AC6. A plain literal-value
    replace (not a pattern like selftest.py's `_redact_secrets`) is correct here because
    ATLAS_TEST_BYPASS is one known, opaque, already-in-hand value, not an unknown-shaped
    leaked token to guess at."""
    if not text or not secret:
        return text
    return text.replace(secret, "[REDACTED]")


class Blocked(Exception):
    """Required config/credentials for a journey are missing, OR (gh#729) a request that DID
    carry ATLAS_TEST_BYPASS still came back 403 -- still "the checker couldn't look", not "the
    product is down". Distinct from a step failing: per sentry.md, this means the CHECKER
    couldn't run, not that the product is down. A blocked journey is excluded from
    results.json's journeys[] entirely so journey_issue_filer.py never files or closes
    anything for it; it still lands in results.json's top-level `blocked` list, optionally
    carrying the response status/headers that caused it (gh#729 AC4)."""

    def __init__(self, message: str, status: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers


# --- catalog ---------------------------------------------------------------------------------

def load_catalog(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def step_text(step: dict, viewport: str) -> tuple[str, str]:
    overrides = (step.get("viewport_overrides") or {}).get(viewport, {})
    action = overrides.get("action", step["action"])
    observable = overrides.get("observable_result", step["observable_result"])
    return action.strip(), observable.strip()


# --- config ------------------------------------------------------------------------------------

class TestUsers:
    def __init__(self, env=None):
        env = env or os.environ
        self.base_url = env.get("PHILANTHROPY_BASE_URL", "https://philanthropy.org")
        self.bypass = env.get("ATLAS_TEST_BYPASS")
        self.fixture_ein = env.get("FIXTURE_EIN")
        self.fixture_claimed_org_url = env.get("FIXTURE_CLAIMED_ORG_URL")
        self.notification_deeplink_url = env.get("NOTIFICATION_DEEPLINK_URL")
        self.fleet_console_url = env.get(
            "FLEET_CONSOLE_URL",
            f"https://dino.luckymachines.co/fleet/{plan_rank.resolve_instance()}",
        )
        self.users = {}
        for name in ("alice", "bob"):
            email, password = env.get(f"{name.upper()}_EMAIL"), env.get(f"{name.upper()}_PASSWORD")
            if email and password:
                self.users[name] = {"email": email, "password": password}

    def require_users(self, *names) -> None:
        missing = [n for n in names if n not in self.users]
        if missing:
            raise Blocked(f"missing test user credentials for: {', '.join(missing)}")

    def require(self, value, name: str):
        if not value:
            raise Blocked(f"missing {name}")
        return value

    def url(self, literal_url: str) -> str:
        """Swaps the scheme+host of a journeys.yaml-literal https://philanthropy.org/... URL
        for the configured base, so this walker can point at a staging host or a local
        fixture without editing the catalog."""
        if literal_url.startswith("http"):
            parts, base = urlsplit(literal_url), urlsplit(self.base_url)
            return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))
        return self.base_url.rstrip("/") + "/" + literal_url.lstrip("/")


# --- playwright helpers (semantic locators, matched to journeys.yaml's own wording) -----------

def expect_visible(locator, timeout=8000):
    locator.first.wait_for(state="visible", timeout=timeout)


def email_field(page):
    return page.get_by_label(re.compile("e-?mail", re.I))


def password_field(page):
    return page.get_by_label(re.compile("password", re.I))


def submit_button(page, pattern=r"sign in|log in|submit"):
    return page.get_by_role("button", name=re.compile(pattern, re.I))


def wait_path_no_longer_contains(page, substr: str, timeout=10000):
    page.wait_for_function("s => !location.pathname.includes(s)", arg=substr, timeout=timeout)


def wait_text_matches(page, pattern: str, timeout=8000):
    page.wait_for_function(
        "p => new RegExp(p, 'i').test(document.body.innerText)", arg=pattern, timeout=timeout
    )


# --- execution context ---------------------------------------------------------------------

class JourneyCtx:
    """One journey, at one viewport. Opens a fresh browser context per named test user on
    first use (contexts, not just pages, so each user gets its own cookie jar -- alice and
    bob must never share a session). `step()` never raises on a failed assertion: it records
    the failure as this step's result and returns False so the calling journey function can
    choose to stop (later steps almost always assume earlier ones succeeded, same as a
    person would not keep going after a broken sign-in) -- but the RUN keeps going regardless,
    per AC3."""

    def __init__(self, journey: dict, viewport: str, dims: dict, browser, users: TestUsers,
                 base_out: Path, run_id: str):
        self.journey = journey
        self.viewport = viewport
        self.dims = dims
        self.browser = browser
        self.users = users
        self.base_out = base_out
        self.run_id = run_id
        self._contexts: dict[str, tuple] = {}
        self.results: list[dict] = []

    def page(self, user: str | None = None):
        key = user or "_anon"
        if key not in self._contexts:
            context = self.browser.new_context(
                viewport={"width": self.dims["width"], "height": self.dims["height"]},
            )
            if self.users.bypass:
                # Per-request, host-scoped injection (gh#729 AC3) -- NOT extra_http_headers on
                # the whole context, which would attach the bypass to every request that
                # context ever makes, including a journey (e.g. fleet-console-loads-with-runs)
                # that navigates the SAME context to a non-philanthropy.org host like
                # FLEET_CONSOLE_URL. A credential must never leak to an unrelated host.
                target_host = urlsplit(self.users.base_url).netloc
                bypass_value = self.users.bypass

                def _inject_bypass(route, request, _host=target_host, _value=bypass_value):
                    if urlsplit(request.url).netloc == _host:
                        route.continue_(headers={**request.headers, BYPASS_HEADER: _value})
                    else:
                        route.continue_()

                context.route("**/*", _inject_bypass)
            self._contexts[key] = (context, context.new_page())
        return self._contexts[key][1]

    def close(self):
        for context, _ in self._contexts.values():
            context.close()
        self._contexts.clear()

    def step(self, index: int, fn, page=None) -> bool:
        step_def = self.journey["steps"][index]
        action, observable = step_text(step_def, self.viewport)
        shot_page = page or next(iter(p for _, p in self._contexts.values()), None)

        status, detail = "pass", None
        try:
            fn()
        except Blocked:
            raise
        except Exception as exc:  # noqa: BLE001 -- a step failing is DATA, not a crash (AC3)
            status = "fail"
            detail = redact_secret(f"{type(exc).__name__}: {exc}", self.users.bypass)

        result = {"index": index, "action": action, "observable_result": observable, "status": status}
        if detail:
            result["detail"] = detail
        if shot_page is not None:
            jid = self.journey["id"]
            shot_dir = self.base_out / self.run_id / "journeys" / jid / self.viewport
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = shot_dir / f"{index}.png"
            try:
                shot_page.screenshot(path=str(shot_path))
                result["screenshot"] = f"{self.base_out}/{self.run_id}/journeys/{jid}/{self.viewport}/{index}.png"
            except Exception:  # noqa: BLE001 -- a screenshot failing must not lose the verdict
                pass
        self.results.append(result)
        return status == "pass"


# --- per-journey implementations, one function per members/sentry/journeys.yaml id -----------

def run_sign_in(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("alice")
    alice = users.users["alice"]
    page = ctx.page("alice")

    def s0():
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        expect_visible(email_field(page))
        expect_visible(password_field(page))
        expect_visible(submit_button(page))

    if not ctx.step(0, s0, page):
        return

    def s1():
        email_field(page).first.fill(alice["email"])
        password_field(page).first.fill(alice["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)

    if not ctx.step(1, s1, page):
        return

    def s2():
        if ctx.viewport == "mobile_390":
            page.get_by_role("button", name=re.compile("menu", re.I)).first.click()
        expect_visible(page.get_by_text(re.compile("sign out", re.I)))
        assert page.get_by_role("link", name=re.compile(r"^sign in$", re.I)).count() == 0

    ctx.step(2, s2, page)


def _redacted_response_headers(response, users: "TestUsers") -> dict | None:
    if response is None:
        return None
    return {k: redact_secret(v, users.bypass) for k, v in dict(response.headers).items()}


def _blocked_for_403(response, users: "TestUsers") -> Blocked | None:
    """Returns a Blocked ready to raise if `response` is the Cloudflare/WAF 403 report pages
    are known to sit behind (gh#729), else None. Shared so every journey that reaches a
    `/990/report/<ein>` URL -- open-990-report's own visit, search-and-open-org's click-through,
    and claim-org-through-verify-screen's own visit -- reads BLOCKED the same way, not only the
    one journey whose implementation happens to call page.goto() on it directly."""
    if response is None or response.status != 403:
        return None
    reason = (
        "report page returned 403 even with ATLAS_TEST_BYPASS configured"
        if users.bypass
        else "report page returned 403 (Cloudflare/WAF challenge) -- ATLAS_TEST_BYPASS not configured"
    )
    return Blocked(reason, status=response.status, headers=_redacted_response_headers(response, users))


def _blocked_for_report_click_through_timeout(users: "TestUsers") -> Blocked:
    """search-and-open-org's step 1 clicked a result row and NO navigation response -- 403 or
    otherwise -- was ever observed on the report-page URL before the click-through gave up
    waiting. `_blocked_for_403` already handles the case where a navigation response DID
    arrive; this is the residual "we never even got a response" case gh#750's PRD (AC3) calls
    out separately -- callers only reach this helper once `_blocked_for_403` has already
    ruled out a 403 (see run_search_and_open_org's s1)."""
    reason = (
        "report page click-through timed out even with ATLAS_TEST_BYPASS configured"
        if users.bypass
        else "report page click-through timed out -- ATLAS_TEST_BYPASS not configured"
    )
    return Blocked(reason)


def run_search_and_open_org(ctx: JourneyCtx):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = ctx.page()

    def s0():
        page.goto(ctx.users.url("https://philanthropy.org/990/?q=hospital"), timeout=15000)
        page.wait_for_function(
            "() => document.querySelectorAll('a[href*=\"/990/report/\"]').length > 0", timeout=10000
        )
        rows = page.locator('a[href*="/990/report/"]')
        assert rows.count() > 0, "no result rows"
        for i in range(rows.count()):
            assert rows.nth(i).inner_text().strip(), "a result row had no org-name text"

    if not ctx.step(0, s0, page):
        return

    def s1():
        first = page.locator('a[href*="/990/report/"]').first
        clicked_text = first.inner_text().strip()

        # gh#750: a plain `"/990/report/" in r.url` substring predicate also matches a
        # sub-resource (e.g. a tracking pixel fired by the same click) if IT resolves before
        # the real navigation response does -- restricting to the main frame's own navigation
        # response is what pins this to the response that actually decided whether the click
        # landed. The listener is registered before click() (not via expect_response's own
        # predicate/timeout) so that first.click()'s own actionability timeout -- a covered or
        # missing button, a real product signal -- propagates as a plain step failure instead
        # of being reclassified as BLOCKED (Non-goal 2). Collecting every matching response
        # (not just the first) and keeping the LAST also means a redirect hop that happens to
        # match the URL substring before the terminal response arrives can't shadow the
        # response that actually decided the outcome.
        nav_responses = []

        def _capture(r):
            if r.frame == page.main_frame and "/990/report/" in r.url and r.request.is_navigation_request():
                nav_responses.append(r)

        page.on("response", _capture)
        try:
            # no_wait_after: click()'s own default behavior also waits for a triggered
            # navigation to settle, up to its own (30s) timeout -- if the checker's own click
            # action reported that timeout instead of the explicit wait_for_url below, a hung
            # connection would propagate as a bare, uncaught TimeoutError (`fail`) before ever
            # reaching the Blocked classification. The explicit wait_for_url call is the sole
            # authority on navigation completion here.
            first.click(no_wait_after=True)
            try:
                page.wait_for_url(re.compile(r"/990/report/"), timeout=10000)
            except PlaywrightTimeoutError:
                nav_response = nav_responses[-1] if nav_responses else None
                blocked = _blocked_for_403(nav_response, ctx.users)
                if blocked:
                    raise blocked
                if nav_response is None:
                    raise _blocked_for_report_click_through_timeout(ctx.users)
                raise  # a non-403 response WAS observed -- a real product signal, stays `fail`
        finally:
            page.remove_listener("response", _capture)

        nav_response = nav_responses[-1] if nav_responses else None
        blocked = _blocked_for_403(nav_response, ctx.users)
        if blocked:
            raise blocked

        heading = page.get_by_role("heading").first.inner_text().strip()
        assert heading, "no org-name heading after opening a result"
        assert clicked_text.split()[0].lower() in heading.lower()

    ctx.step(1, s1, page)


def run_open_990_report(ctx: JourneyCtx):
    ein = ctx.users.require(ctx.users.fixture_ein, "FIXTURE_EIN")
    page = ctx.page()

    def s0():
        response = page.goto(ctx.users.url(f"https://philanthropy.org/990/report/{ein}"), timeout=15000)
        blocked = _blocked_for_403(response, ctx.users)
        if blocked:
            raise blocked
        expect_visible(page.get_by_role("heading"))
        wait_text_matches(page, r"\$[0-9]|revenue|expense", timeout=8000)

    if not ctx.step(0, s0, page):
        return

    def s1():
        tabs = page.get_by_role("tab", name=re.compile("financ", re.I))
        if tabs.count() > 0:
            tabs.first.click()
        wait_text_matches(page, r"\$[0-9]", timeout=8000)

    ctx.step(1, s1, page)


def run_claim_org_through_verify_screen(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("alice")
    ein = users.require(users.fixture_ein, "FIXTURE_EIN")
    alice = users.users["alice"]
    page = ctx.page("alice")

    def sign_in_alice():
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        email_field(page).first.fill(alice["email"])
        password_field(page).first.fill(alice["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)

    def s0():
        sign_in_alice()
        response = page.goto(users.url(f"https://philanthropy.org/990/report/{ein}"), timeout=15000)
        blocked = _blocked_for_403(response, users)
        if blocked:
            raise blocked
        page.get_by_role("button", name=re.compile("claim this organization|claim", re.I)).first.click()
        wait_text_matches(page, r"claim", timeout=8000)

    if not ctx.step(0, s0, page):
        return

    def s1():
        for label_pattern in ("name", "role|title", "relationship"):
            field = page.get_by_label(re.compile(label_pattern, re.I))
            if field.count() > 0:
                field.first.fill("Sentry QA")
        page.get_by_role("button", name=re.compile("continue|next|submit", re.I)).first.click()

    if not ctx.step(1, s1, page):
        return

    def s2():
        wait_text_matches(page, r"verify|verification|upload|confirm your (email|identity)", timeout=8000)

    ctx.step(2, s2, page)


def run_verified_org_checkout_to_stripe(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("alice")
    admin_path = users.require(users.fixture_claimed_org_url, "FIXTURE_CLAIMED_ORG_URL")
    alice = users.users["alice"]
    page = ctx.page("alice")

    def s0():
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        email_field(page).first.fill(alice["email"])
        password_field(page).first.fill(alice["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)
        page.goto(users.url(admin_path), timeout=15000)
        page.get_by_role("button", name=re.compile("upgrade to verified|upgrade", re.I)).first.click()
        wait_text_matches(page, r"plan|checkout", timeout=8000)

    if not ctx.step(0, s0, page):
        return

    def s1():
        plan = page.get_by_text(re.compile("verified org", re.I))
        if plan.count() > 0:
            plan.first.click()
        page.get_by_role("button", name=re.compile("continue|checkout", re.I)).first.click()
        page.wait_for_url(re.compile(r"checkout\.stripe\.com"), timeout=15000)

    ctx.step(1, s1, page)


def run_message_send_and_read_receipt(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("alice", "bob")
    alice_page, bob_page = ctx.page("alice"), ctx.page("bob")
    marker = f"sentry-{ctx.run_id}-{int(time.time())}"

    def sign_in(page, creds):
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        email_field(page).first.fill(creds["email"])
        password_field(page).first.fill(creds["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)

    def s0():
        sign_in(alice_page, users.users["alice"])
        composer = alice_page.get_by_role("textbox")
        composer.first.fill(marker)
        alice_page.get_by_role("button", name=re.compile("send", re.I)).first.click()
        wait_text_matches(alice_page, re.escape(marker), timeout=5000)

    if not ctx.step(0, s0, alice_page):
        return

    def s1():
        sign_in(bob_page, users.users["bob"])
        bob_page.reload()
        wait_text_matches(bob_page, re.escape(marker), timeout=30000)

    if not ctx.step(1, s1, bob_page):
        return

    def s2():
        bob_page.bring_to_front()  # left open/focused so the message is marked read

    ctx.step(2, s2, bob_page)

    def s3():
        alice_page.reload()
        wait_text_matches(alice_page, r"read", timeout=30000)

    ctx.step(3, s3, alice_page)


def run_open_thread_from_notification_link_and_send(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("bob")
    deeplink = users.require(users.notification_deeplink_url, "NOTIFICATION_DEEPLINK_URL")
    bob = users.users["bob"]
    page = ctx.page("bob")
    marker = f"sentry-reply-{ctx.run_id}-{int(time.time())}"

    def s0():
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        email_field(page).first.fill(bob["email"])
        password_field(page).first.fill(bob["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)
        page.goto(users.url(deeplink), timeout=15000)

    if not ctx.step(0, s0, page):
        return

    def s1():
        composer = page.get_by_role("textbox")
        composer.first.fill(marker)
        page.get_by_role("button", name=re.compile("send", re.I)).first.click()
        wait_text_matches(page, re.escape(marker), timeout=5000)

    ctx.step(1, s1, page)


def run_typing_indicator(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("alice", "bob")
    alice_page, bob_page = ctx.page("alice"), ctx.page("bob")

    def sign_in(page, creds):
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        email_field(page).first.fill(creds["email"])
        password_field(page).first.fill(creds["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)

    sign_in(alice_page, users.users["alice"])
    sign_in(bob_page, users.users["bob"])

    def s0():
        alice_page.get_by_role("textbox").first.type("typing...", delay=50)
        wait_text_matches(bob_page, "typing", timeout=5000)

    if not ctx.step(0, s0, bob_page):
        return

    def s1():
        alice_page.get_by_role("textbox").first.blur()
        time.sleep(5)
        assert not re.search("typing", bob_page.locator("body").inner_text(), re.I)

    ctx.step(1, s1, bob_page)


def run_sign_out(ctx: JourneyCtx):
    users = ctx.users
    users.require_users("alice")
    alice = users.users["alice"]
    page = ctx.page("alice")

    def s0():
        page.goto(users.url("https://philanthropy.org/login"), timeout=15000)
        email_field(page).first.fill(alice["email"])
        password_field(page).first.fill(alice["password"])
        submit_button(page).first.click()
        wait_path_no_longer_contains(page, "/login", timeout=10000)
        page.get_by_text(re.compile("sign out", re.I)).first.click()
        expect_visible(page.get_by_role("link", name=re.compile(r"^sign in$", re.I)))

    if not ctx.step(0, s0, page):
        return

    def s1():
        page.goto(users.url("https://philanthropy.org/account"), timeout=15000)
        wait_path_no_longer_contains(page, "/account", timeout=8000)

    ctx.step(1, s1, page)


def run_fleet_console_loads_with_runs(ctx: JourneyCtx):
    page = ctx.page()

    def s0():
        page.goto(ctx.users.fleet_console_url, timeout=15000)
        wait_text_matches(page, r"runs|needs you", timeout=8000)

    if not ctx.step(0, s0, page):
        return

    def s1():
        # gh#724: the console's real markup renders each agent as
        # `<div class="row" data-agent="...">` -- the previous selector list
        # ([data-run],[data-testid="run-row"],tr,li) matched none of it.
        page.wait_for_function(
            "() => document.querySelectorAll('[data-agent]').length > 0",
            timeout=8000,
        )

    ctx.step(1, s1, page)


JOURNEY_RUNNERS = {
    "sign-in": run_sign_in,
    "search-and-open-org": run_search_and_open_org,
    "open-990-report": run_open_990_report,
    "claim-org-through-verify-screen": run_claim_org_through_verify_screen,
    "verified-org-checkout-to-stripe": run_verified_org_checkout_to_stripe,
    "message-send-and-read-receipt": run_message_send_and_read_receipt,
    "open-thread-from-notification-link-and-send": run_open_thread_from_notification_link_and_send,
    "typing-indicator": run_typing_indicator,
    "sign-out": run_sign_out,
    "fleet-console-loads-with-runs": run_fleet_console_loads_with_runs,
}


# --- orchestration ---------------------------------------------------------------------------

def run_all(catalog: dict, users: TestUsers, browser, base_out: Path, run_id: str,
            journey_filter=None) -> tuple[list[dict], list[dict]]:
    """Walks every journey in the catalog, sequentially, at each of its viewports,
    sequentially. Never lets one journey's exception stop the rest (AC3) -- a runner bug is
    caught same as a step assertion failure and recorded, not raised."""
    journeys_out, blocked = [], []
    for journey in catalog["journeys"]:
        if journey_filter and journey["id"] not in journey_filter:
            continue
        runner = JOURNEY_RUNNERS.get(journey["id"])
        if runner is None:
            print(f"journey_walker: no implementation for '{journey['id']}', skipping", file=sys.stderr)
            continue
        viewports = journey.get("viewports") or ["desktop"]
        for viewport in viewports:
            dims = catalog["viewports"][viewport]
            ctx = JourneyCtx(journey, viewport, dims, browser, users, base_out, run_id)
            try:
                runner(ctx)
            except Blocked as b:
                reason = redact_secret(str(b), users.bypass)
                print(f"journey_walker: BLOCKED {journey['id']} [{viewport}]: {reason}", file=sys.stderr)
                entry = {"id": journey["id"], "viewport": viewport, "reason": reason}
                if b.status is not None:
                    entry["response"] = {"status": b.status, "headers": b.headers or {}}
                blocked.append(entry)
                ctx.close()
                continue
            except Exception:  # noqa: BLE001 -- a runner bug must not end the whole pass
                traceback.print_exc()
                ctx.results.append({
                    "index": len(ctx.results),
                    "action": "(journey_walker internal error)",
                    "observable_result": "(journey_walker internal error)",
                    "status": "fail",
                    "detail": traceback.format_exc()[-2000:],
                })
            ctx.close()
            if not ctx.results:
                continue
            out_id = journey["id"] if viewport == "desktop" else f"{journey['id']}--{viewport}"
            out_name = journey["name"] if viewport == "desktop" else f"{journey['name']} ({viewport})"
            journeys_out.append({"id": out_id, "name": out_name, "steps": ctx.results})
    return journeys_out, blocked


def summarize(journeys_out: list[dict], blocked: list[dict]) -> dict:
    steps_passed = sum(1 for j in journeys_out for s in j["steps"] if s["status"] == "pass")
    steps_failed = sum(1 for j in journeys_out for s in j["steps"] if s["status"] == "fail")
    journeys_passed = sum(1 for j in journeys_out if all(s["status"] == "pass" for s in j["steps"]))
    journeys_failed = len(journeys_out) - journeys_passed
    return {
        "journeys_passed": journeys_passed,
        "journeys_failed": journeys_failed,
        "journeys_blocked": len(blocked),
        "steps_passed": steps_passed,
        "steps_failed": steps_failed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", type=Path, default=ROOT / "members" / "sentry" / "journeys.yaml")
    ap.add_argument("--out", type=Path, default=Path("qa-out"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--deploy-sha", default=os.environ.get("DEPLOY_SHA", ""))
    ap.add_argument("--journeys", nargs="*", default=None, help="only run these journey ids (debugging)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    catalog = load_catalog(args.catalog)
    users = TestUsers()
    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], headless=not args.headed)
        try:
            journeys_out, blocked = run_all(catalog, users, browser, args.out, run_id, args.journeys)
        finally:
            browser.close()

    summary = summarize(journeys_out, blocked)
    results = {"run": run_id, "deploy_sha": args.deploy_sha, "journeys": journeys_out,
               "blocked": blocked, "summary": summary}
    results_dir = args.out / run_id / "journeys"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / "results.json"
    results_path.write_text(json.dumps(results, indent=2))

    print(f"journey_walker: wrote {results_path}")
    print(
        f"journey_walker: {summary['journeys_passed']} journeys passed, "
        f"{summary['journeys_failed']} failed, {summary['journeys_blocked']} blocked "
        f"({summary['steps_passed']} steps passed, {summary['steps_failed']} failed)"
    )
    return 1 if summary["journeys_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
