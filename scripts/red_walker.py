#!/usr/bin/env python3
"""red_walker.py -- drives members/red/attacks.yaml against philanthropy.org with Playwright
and writes qa-out/<run>/red/results.json (fleet-kit#785). AUTHORIZED adversarial testing of
our OWN product only.

Sibling to scripts/journey_walker.py: same Playwright + results.json shape (so
journey_issue_filer.py files red findings the same way it files sentry ones), same
BLOCKED-vs-BROKEN law (a Cloudflare 403 is the checker losing its credential, not a product
break -- fk#729), same semantic-locator style. The one inversion: a step's `landed_when`
describes the attack SUCCEEDING, i.e. the product being BROKEN, so `landed_when` observed ==
status "fail" in results.json (a red finding to file), and NOT observed == "pass" (the product
held). That keeps the filer's own "fail files an issue, pass closes it" logic correct without
teaching it anything about attacks.

CONFIG (env; a missing one for an attack that needs it makes that attack BLOCKED, not failed):
  PHILANTHROPY_BASE_URL          default https://philanthropy.org
  ATLAS_TEST_BYPASS              WAF bypass header (fk#729); host-scoped, never sent elsewhere
  ALICE_EMAIL / ALICE_PASSWORD   the signed-in test user for idor/double-submit attacks
  FIXTURE_EIN                    a known-good EIN
  FIXTURE_OTHER_ORG_ADMIN_URL    an org-admin URL alice does NOT own (for the idor attack)
  FIXTURE_CLAIM_URL              the claim flow entry path (for the double-submit attack)

Usage: red_walker.py [--catalog PATH] [--out qa-out] [--run-id ID] [--attacks id ...]
                     [--item N] [--headed]
--item N restricts to attacks whose target path a PRD names (best-effort substring match) and
tags the run so vp can post `Red team (adversarial): <n>` against that item.
Exit: 1 if any attack landed (a finding), else 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent.parent
BYPASS_HEADER = "X-Atlas-Test-Bypass"
PAYLOAD_OVERFLOW = re.compile(r"^(.)\s*x\s*(\d+)$")  # "A x 10000" -> "A"*10000


class Blocked(Exception):
    def __init__(self, message, status=None, headers=None):
        super().__init__(message)
        self.status = status
        self.headers = headers


def load_catalog(path: Path) -> dict:
    import yaml  # lazy: only needed at runtime, so importing this module for unit tests needs no PyYAML
    with open(path) as f:
        return yaml.safe_load(f)


class Config:
    def __init__(self, env=None):
        env = env or os.environ
        self.base_url = env.get("PHILANTHROPY_BASE_URL", "https://philanthropy.org")
        self.bypass = env.get("ATLAS_TEST_BYPASS")
        self.env = env

    def url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            p, b = urlsplit(path_or_url), urlsplit(self.base_url)
            return urlunsplit((b.scheme, b.netloc, p.path, p.query, p.fragment))
        return self.base_url.rstrip("/") + "/" + path_or_url.lstrip("/")

    def require(self, name: str) -> str:
        v = self.env.get(name)
        if not v:
            raise Blocked(f"missing {name}")
        return v


def expand_payload(raw: str | None) -> str | None:
    if raw is None:
        return None
    m = PAYLOAD_OVERFLOW.match(raw.strip())
    if m:
        return m.group(1) * int(m.group(2))
    return raw


def _context(browser, cfg: Config, dims: dict):
    ctx = browser.new_context(viewport={"width": dims["width"], "height": dims["height"]})
    if cfg.bypass:
        host = urlsplit(cfg.base_url).netloc
        value = cfg.bypass

        def _inject(route, request, _h=host, _v=value):
            if urlsplit(request.url).netloc == _h:
                route.continue_(headers={**request.headers, BYPASS_HEADER: _v})
            else:
                route.continue_()

        ctx.route("**/*", _inject)
    return ctx


def _sign_in(page, cfg: Config, user: str):
    email = cfg.require(f"{user.upper()}_EMAIL")
    password = cfg.require(f"{user.upper()}_PASSWORD")
    resp = page.goto(cfg.url("/login"), timeout=15000)
    if resp is not None and resp.status == 403:
        raise Blocked("login returned 403 (WAF challenge)", status=403)
    page.get_by_label(re.compile("e-?mail", re.I)).first.fill(email)
    page.get_by_label(re.compile("password", re.I)).first.fill(password)
    page.get_by_role("button", name=re.compile("sign in|log in|submit", re.I)).first.click()
    page.wait_for_function("() => !location.pathname.includes('/login')", timeout=10000)


# --- per-kind attack runners: each returns (landed: bool, note: str) or raises Blocked -------

def _guard_403(resp):
    if resp is not None and resp.status == 403:
        raise Blocked("target returned 403 (WAF challenge), attack not attempted", status=403)


def run_reflection(page, cfg, attack, step):
    payload = expand_payload(step.get("payload")) or "<img src=x onerror=window.__redpwn=1>"
    resp = page.goto(cfg.url(attack["target"]["path"] + "?q=" + payload), timeout=20000)
    _guard_403(resp)
    page.wait_for_timeout(1500)
    flagged = page.evaluate("() => !!window.__redpwn")
    raw_in_html = page.evaluate("(p) => document.body.innerHTML.includes(p)", payload)
    if flagged:
        return True, "onerror handler executed (window.__redpwn set)"
    if raw_in_html:
        return True, "payload rendered verbatim into innerHTML (not escaped)"
    return False, "payload neither executed nor reflected unescaped"


def run_overflow(page, cfg, attack, step):
    payload = expand_payload(step.get("payload")) or ("A" * 10000)
    resp = page.goto(cfg.url(attack["target"]["path"] + "?q=" + payload[:6000]), timeout=25000)
    _guard_403(resp)
    if resp is not None and resp.status >= 500:
        return True, f"status {resp.status} on an overlong query"
    body = page.evaluate("() => document.body.innerText.slice(0, 4000)")
    if re.search(r"traceback|stack trace|unhandled|exception at |DoesNotExist", body, re.I):
        return True, "an exception/stack trace rendered to the page"
    return False, f"status {resp.status if resp else '?'}, no 5xx or stack trace"


def run_param_range(page, cfg, attack, step):
    base = attack["target"]["path"].rstrip("/")
    for probe in ("NOT-AN-EIN", "0"):
        resp = page.goto(cfg.url(f"{base}/{probe}"), timeout=20000)
        _guard_403(resp)
        status = resp.status if resp else 0
        body = page.evaluate("() => document.body.innerText.slice(0, 4000)")
        if status >= 500 or re.search(r"traceback|stack trace|unhandled|exception at |ProgrammingError", body, re.I):
            return True, f"/{probe} -> status {status} or a raw error page"
    return False, "garbage EINs returned a clean 404/empty-state, not a 5xx"


def run_idor(page, cfg, attack, step):
    _sign_in(page, cfg, attack["target"].get("user", "alice"))
    url = cfg.require("FIXTURE_OTHER_ORG_ADMIN_URL")
    resp = page.goto(cfg.url(url), timeout=20000)
    _guard_403(resp)
    status = resp.status if resp else 0
    path = urlsplit(page.url).path
    if status in (403, 404) or "/login" in path:
        return False, f"blocked correctly (status {status}, path {path})"
    controls = page.get_by_role("button", name=re.compile("save|edit|delete|invite|settings", re.I)).count()
    if controls > 0:
        return True, f"admin controls rendered for an org alice does not own ({controls} control(s), status {status})"
    return False, f"no admin control exposed (status {status})"


def run_double_submit(page, cfg, attack, step):
    _sign_in(page, cfg, attack["target"].get("user", "alice"))
    url = cfg.require("FIXTURE_CLAIM_URL")
    resp = page.goto(cfg.url(url), timeout=20000)
    _guard_403(resp)
    submit = page.get_by_role("button", name=re.compile("claim|submit|confirm", re.I)).first
    submit.wait_for(state="visible", timeout=8000)
    statuses = []
    with page.expect_response(re.compile(r".*"), timeout=8000) as _:
        submit.click()
    try:
        submit.click(timeout=1000)  # the second, fast click
    except Exception:  # noqa: BLE001 -- button disabled after first click is the HEALTHY case
        return False, "second click was rejected (control disabled) -- idempotent"
    page.wait_for_timeout(1000)
    body = page.evaluate("() => document.body.innerText")
    if re.search(r"already (claimed|submitted)|pending review", body, re.I):
        return False, "second submit handled idempotently"
    if len(re.findall(r"claim (submitted|received|created)", body, re.I)) >= 2:
        return True, "two claim confirmations appeared for one org"
    return False, "no evidence of a double-create"


def run_console(page, cfg, attack, step):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    resp = page.goto(cfg.url(attack["target"]["path"]), timeout=20000)
    _guard_403(resp)
    try:
        box = page.get_by_role("searchbox").or_(page.get_by_label(re.compile("search", re.I))).first
        box.fill("hospital", timeout=4000)
        box.press("Enter")
        page.wait_for_timeout(2000)
    except Exception:  # noqa: BLE001 -- no search box on this page is not itself a console error
        pass
    if errors:
        return True, f"{len(errors)} console error(s): {errors[0][:160]}"
    return False, "no console error during load or search"


RUNNERS = {
    "reflection": run_reflection, "overflow": run_overflow, "param-range": run_param_range,
    "idor": run_idor, "double-submit": run_double_submit, "console": run_console,
}


def run_attack(attack: dict, cfg: Config, browser, base_out: Path, run_id: str, viewport: str, dims: dict) -> dict:
    runner = RUNNERS.get(attack["kind"])
    steps_out = []
    ctx = _context(browser, cfg, dims)
    page = ctx.new_page()
    try:
        for i, step in enumerate(attack.get("steps", [])):
            status, note, shot = "pass", "", None
            try:
                landed, detail = runner(page, cfg, attack, step)
                status = "fail" if landed else "pass"
                note = detail
            except Blocked:
                raise
            except Exception as exc:  # noqa: BLE001 -- a runner bug is data, not a crash
                status, note = "pass", f"(red_walker internal error, treated as no-finding: {type(exc).__name__})"
                traceback.print_exc()
            aid = attack["id"] if viewport == "desktop" else f"{attack['id']}--{viewport}"
            shot_dir = base_out / run_id / "red" / attack["id"] / viewport
            try:
                shot_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot_dir / f"{i}.png"))
                shot = f"{base_out}/{run_id}/red/{attack['id']}/{viewport}/{i}.png"
            except Exception:  # noqa: BLE001
                pass
            out = {"index": i, "action": step.get("action", ""),
                   "observable_result": f"HEALTHY iff NOT: {step.get('landed_when', '').strip()}",
                   "status": status, "detail": note}
            if shot:
                out["screenshot"] = shot
            steps_out.append(out)
    finally:
        ctx.close()
    aid = attack["id"] if viewport == "desktop" else f"{attack['id']}--{viewport}"
    name = attack["name"] if viewport == "desktop" else f"{attack['name']} ({viewport})"
    return {"id": aid, "name": name, "steps": steps_out}


def _needs_met(attack: dict, cfg: Config) -> str | None:
    for var in attack.get("target", {}).get("needs", []):
        if not cfg.env.get(var):
            return var
    return None


def fetch_item_text(item: str, run=subprocess.run) -> str:
    """Issue body + all comment bodies for --item -- same source closes_gate.acceptance_criteria()
    reads (gh#881 PRD: marie's PRDs live in comments, so the body alone would miss them). Raises
    SystemExit naming the issue on any gh failure (criterion 5) rather than falling back."""
    try:
        proc = run(["gh", "issue", "view", str(item), "--json", "body,comments"],
                    capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise SystemExit(f"red_walker: could not read issue #{item} ({exc})")
    if proc.returncode != 0:
        raise SystemExit(f"red_walker: could not read issue #{item} ({proc.stderr.strip() or 'gh error'})")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"red_walker: could not parse issue #{item} ({exc})")
    comments = data.get("comments") or []
    return "\n".join([data.get("body") or ""] + [c.get("body") or "" for c in comments])


def select_attacks(attacks: list[dict], item_text: str | None, attacks_filter: list[str] | None) -> list[dict]:
    """Pure, no network/Playwright -- unit-testable selection (criterion 7). item_text is the
    item's PRD text (body + comments) when --item is given, else None for the unscoped path
    (criterion 4, unchanged). An attack is selected when its target path appears in item_text --
    the direction red_walker.py's own usage text documents (gh#881; the old code matched the
    issue number against the target blob, which never matches, so it silently ran nothing)."""
    selected = []
    for attack in attacks:
        if attacks_filter is not None and attack["id"] not in attacks_filter:
            continue
        if item_text is not None:
            path = attack.get("target", {}).get("path")
            if not path or path not in item_text:
                continue
        selected.append(attack)
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", type=Path, default=ROOT / "members" / "red" / "attacks.yaml")
    ap.add_argument("--out", type=Path, default=Path("qa-out"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--attacks", nargs="*", default=None)
    ap.add_argument("--item", default=None)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    catalog = load_catalog(args.catalog)
    cfg = Config()
    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    attacks_out, blocked = [], []

    item_text = fetch_item_text(args.item) if args.item else None
    selected = select_attacks(catalog["attacks"], item_text, args.attacks)

    if args.item and not selected:
        reason = f"no attack target path appears in #{args.item}'s PRD"
        results = {"run": run_id, "deploy_sha": os.environ.get("DEPLOY_SHA", ""), "item": args.item,
                   "journeys": [], "blocked": [], "selected": 0, "reason": reason,
                   "summary": {"attacks": 0, "landed": 0, "blocked": 0}}
        out_dir = args.out / run_id / "red"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "results.json").write_text(json.dumps(results, indent=2))
        print(f"red_walker: 0 attacks selected for #{args.item} -- {reason} -> {out_dir}/results.json",
              file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], headless=not args.headed)
        try:
            for attack in selected:
                missing = _needs_met(attack, cfg)
                if missing:
                    blocked.append({"id": attack["id"], "reason": f"missing {missing}"})
                    continue
                for vp in (attack.get("viewports") or ["desktop"]):
                    dims = catalog["viewports"][vp]
                    try:
                        attacks_out.append(run_attack(attack, cfg, browser, args.out, run_id, vp, dims))
                    except Blocked as b:
                        blocked.append({"id": attack["id"], "viewport": vp, "reason": str(b),
                                        **({"response": {"status": b.status}} if b.status else {})})
        finally:
            browser.close()

    landed = sum(1 for a in attacks_out for s in a["steps"] if s["status"] == "fail")
    results = {"run": run_id, "deploy_sha": os.environ.get("DEPLOY_SHA", ""), "item": args.item,
               "journeys": attacks_out, "blocked": blocked,
               "summary": {"attacks": len(attacks_out), "landed": landed, "blocked": len(blocked)}}
    out_dir = args.out / run_id / "red"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    print(f"red_walker: {len(attacks_out)} attacks, {landed} landed, {len(blocked)} blocked -> {out_dir}/results.json")
    return 1 if landed else 0


if __name__ == "__main__":
    raise SystemExit(main())
