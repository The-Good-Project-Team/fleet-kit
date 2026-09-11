#!/usr/bin/env python3
"""ui_render_check -- gh#809 first slice: give the fleet a pre-merge look at its own UI.

WHAT THIS IS. Before this, nothing in the fleet ever rendered a page: judge-judy reads diffs as
text (`tools: none`, by design), red and sentry both run post-merge against live prod. This
script is the missing pre-merge step: for a PR that touches a template/static/route/CSS file,
boot this repo's OWN console (`scripts/fleet_view_server.py`, which serves `fleet_home.html`/
`fleet_view.html` -- the only two rendered surfaces this repo has, per gh#645/gh#573) locally,
screenshot the affected page at phone width AND desktop width, and list any uncaught console
errors.

ROUND 1 (VP review, gh#809 comment): a screenshot buried in a runner's local filesystem is not
evidence a reviewer can open, a 25s boot timeout undershot this server's own documented ~27s
cold path, phone-only rendering missed the desktop-shaped half of the 19 rework PRs this issue
was filed over, and a fixed 500ms wait after `load` reported a clean console on a page that was
still filling in. This revision: the report links the CI run itself so no screenshot's only
reference is a local path; `boot_server`'s default timeout is 45s; every surface renders at both
390x844 and 1280x800; and each render polls body text until it stops growing (bounded at 8s)
before screenshotting, and says how long it waited.

gh#809's own PRD flags "what surface a PR renders against before merge" as UNKNOWN, since this
repo has no per-PR preview deployment, and names "a locally-booted app in CI" and "restricting
v1 to surfaces servable from a static checkout" as the two live options, deferring the pick to
whoever builds this. This picks the first: fleet_view_server.py boots from a plain checkout with
no external deps beyond `gh` (already how it runs in production), so a local boot is the
faithful rendering of "this PR's version of the console" -- not a guess and not new invention.

WHY EVIDENCE, NOT A VERDICT. gh#809's non-goals explicitly exclude a blocking "looks wrong"
verdict -- that needs a human decision this pass doesn't make. So this script always exits 0:
a blank render or a caught console error is reported IN the output (criteria 4 and 2), never
turned into a failing process. The CI job that calls this is likewise not a required check
(see ci.yml's own comment on gh#804: a required check must never depend on booting a server or
reaching `gh`, which this does) -- screenshots reach the PR as a comment, which is where `vp`
(criterion 6) and a human both already read.

WHAT THIS DELIBERATELY LEAVES OPEN (say so in the PR, don't guess): whether "looks wrong" ever
blocks a merge (gh#809's own second UNKNOWN), and inline image embedding (this links to the CI
run's artifact rather than hotlinking the PNG itself -- VP's round 1 review named both as valid
and flagged the embed path as the part it was least sure GitHub Actions comments support).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Known rendered surfaces in this repo (gh#645/gh#573: fleet_home.html is the console home,
# fleet_view.html is the superseded classic view fleet_view_server.py still serves at
# /classic). A route file that SERVES a surface counts as touching it even with no HTML edit.
SURFACE_MAP = {
    "scripts/fleet_home.html": ("/", "fleet_home"),
    "scripts/fleet_view.html": ("/classic", "fleet_view_classic"),
    "scripts/fleet_view_server.py": ("/", "fleet_home"),
    "scripts/status_page.py": ("/status", "status_page"),
}
ROUTE_TOUCHING_SUFFIXES = (".html", ".css")

BLANK_TEXT_THRESHOLD = 40  # chars of rendered body text below which a page counts as blank
VIEWPORTS = {
    "390x844": {"width": 390, "height": 844},
    "1280x800": {"width": 1280, "height": 800},
}

# gh#809 VP review round 1, fix 4: a fixed 500ms wait after `load` reported a clean console on a
# page that was still filling in -- measured on /classic at 1280x800, 1,189 chars of body text at
# 500ms vs 1,785 at 5s. Poll body text length instead and stop once it holds steady, bounded so a
# page that never settles (e.g. a live-updating dashboard) still screenshots within a fixed budget.
SETTLE_MAX_WAIT = 8.0
SETTLE_POLL_INTERVAL = 0.5
SETTLE_STABLE_READS = 2


class ServerUnavailable(Exception):
    """The local console could not be booted or reached -- criterion 5: report `unavailable`,
    never fail the PR over it."""


def select_surfaces(changed_files: list[str]) -> dict[str, str]:
    """changed_files -> {url_path: surface_name} for every affected rendered surface.

    Pure and dependency-free on purpose (test_ui_render_check.py pins this against real
    file-list fixtures without needing playwright or a running server)."""
    surfaces: dict[str, str] = {}
    for f in changed_files:
        if f in SURFACE_MAP:
            path, name = SURFACE_MAP[f]
            surfaces[path] = name
        elif f.endswith(ROUTE_TOUCHING_SUFFIXES):
            surfaces.setdefault("/", "fleet_home")
    return surfaces


def changed_files_vs(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return [l for l in out.stdout.splitlines() if l.strip()]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# gh#809 VP review round 1, fix 2: fleet_view_server.py runs two synchronous `gh`-backed warms
# (poll_gh_state, _backlog_history_payload -- fleet_view_server.py:1764 documents the second at
# ~27s) before it binds the port. VP measured 30.6s cold-boot-to-first-/status here; the old 25s
# default undershot that and silently degraded to `unavailable` on every cold run.
def boot_server(log_dir: Path, port: int, timeout: float = 45.0) -> subprocess.Popen:
    env = dict(os.environ)
    env["FLEET_REPO"] = str(REPO_ROOT)
    env["FLEET_LOG_DIR"] = str(log_dir)
    env["FLEET_VIEW_PORT"] = str(port)
    log_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "scripts" / "fleet_view_server.py")],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise ServerUnavailable(
                f"fleet_view_server exited early (rc={proc.returncode}): {(err or '').strip()[-500:]}"
            )
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.5)
    proc.kill()
    raise ServerUnavailable(f"fleet_view_server did not answer /status within {timeout}s: {last_err}")


def render_surface(url: str, out_png: Path, viewport: dict) -> dict:
    from playwright.sync_api import sync_playwright  # local import: keep this optional for
    # callers (and test_ui_render_check.py) that only need select_surfaces()'s pure logic.

    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport=viewport)
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.goto(url, timeout=30000, wait_until="load")
            settle_start = time.monotonic()
            prev_len = -1
            stable_reads = 0
            text = page.inner_text("body") or ""
            while time.monotonic() - settle_start < SETTLE_MAX_WAIT:
                cur_len = len(text.strip())
                if cur_len == prev_len:
                    stable_reads += 1
                    if stable_reads >= SETTLE_STABLE_READS:
                        break
                else:
                    stable_reads = 0
                prev_len = cur_len
                page.wait_for_timeout(int(SETTLE_POLL_INTERVAL * 1000))
                text = page.inner_text("body") or ""
            settle_seconds = round(time.monotonic() - settle_start, 2)
            out_png.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_png))
        finally:
            browser.close()
    return {
        "console_errors": console_errors,
        "text_len": len(text.strip()),
        "settle_seconds": settle_seconds,
    }


def _write_report(out_dir: Path, report: dict, run_url: str | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))

    lines = ["## UI render check (gh#809)", ""]
    if report.get("unavailable"):
        lines.append(f"**unavailable** -- {report['unavailable']}")
        lines.append("")
        lines.append("This does not block the PR (gh#809 criterion 5).")
    elif not report.get("surfaces"):
        lines.append("No template/static/route/CSS files changed -- nothing rendered.")
    else:
        # gh#809 VP review round 1, fix 1: a filesystem path is meaningless to a reader with no
        # shell into the runner -- give them a real click-through to the run's artifacts before
        # any path appears below, so no screenshot's only reference is a local path.
        if run_url:
            lines.append(f"**Screenshots & full report:** {run_url}")
            lines.append("")
        for path, s in sorted(report["surfaces"].items()):
            lines.append(f"### `{path}` ({s['name']})")
            if s.get("error"):
                lines.append(f"- render error: {s['error']}")
            else:
                for vp_label, r in sorted(s.get("renders", {}).items()):
                    if r.get("error"):
                        lines.append(f"- {vp_label}: render error: {r['error']}")
                        continue
                    lines.append(f"- screenshot ({vp_label}): `{r['screenshot']}`, "
                                 f"waited {r['settle_seconds']}s for the page to settle")
                    if r.get("blank"):
                        lines.append(
                            f"  - **BLANK RENDER** -- only {r['text_len']} chars of body text "
                            f"(threshold {BLANK_TEXT_THRESHOLD})"
                        )
                    errs = r.get("console_errors") or []
                    if errs:
                        lines.append(f"  - console errors ({len(errs)}):")
                        for e in errs[:20]:
                            lines.append(f"    - `{e}`")
                    else:
                        lines.append("  - console errors: none")
            lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="git ref to diff HEAD against")
    ap.add_argument("--out", default="qa-out/ui_render", help="output directory for report + screenshots")
    ap.add_argument("--changed-files", nargs="*", default=None,
                     help="override the changed-file list instead of computing it from --base")
    ap.add_argument("--run-url", default=None,
                     help="link to this CI run, included in the report so a reader with no "
                          "shell into the runner has a one-click path to the screenshots "
                          "(gh#809 VP review round 1, fix 1)")
    args = ap.parse_args(argv)

    changed = args.changed_files if args.changed_files is not None else changed_files_vs(args.base)
    surfaces = select_surfaces(changed)
    out_dir = Path(args.out)

    if not surfaces:
        print("ui_render_check: no template/static/route/CSS files changed; skipping.")
        return 0

    log_dir = Path(tempfile.mkdtemp(prefix="ui_render_check_"))
    port = _free_port()
    report: dict = {"surfaces": {}}
    try:
        proc = boot_server(log_dir, port)
    except ServerUnavailable as exc:
        report["unavailable"] = str(exc)
        _write_report(out_dir, report, run_url=args.run_url)
        print(f"ui_render_check: UNAVAILABLE -- {exc}")
        return 0

    try:
        for path, name in sorted(surfaces.items()):
            url = f"http://127.0.0.1:{port}{path}"
            renders: dict = {}
            for vp_label, viewport in VIEWPORTS.items():
                png = out_dir / f"{name}_{vp_label}.png"
                try:
                    r = render_surface(url, png, viewport)
                    blank = r["text_len"] < BLANK_TEXT_THRESHOLD
                    renders[vp_label] = {
                        "screenshot": str(png),
                        "console_errors": r["console_errors"],
                        "text_len": r["text_len"], "blank": blank,
                        "settle_seconds": r["settle_seconds"],
                    }
                    if blank:
                        print(f"ui_render_check: BLANK RENDER at {path} ({vp_label}, "
                              f"{r['text_len']} chars)")
                except Exception as exc:  # noqa: BLE001
                    renders[vp_label] = {"error": str(exc)}
            report["surfaces"][path] = {"name": name, "renders": renders}
    finally:
        proc.kill()
        proc.wait(timeout=10)

    _write_report(out_dir, report, run_url=args.run_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
