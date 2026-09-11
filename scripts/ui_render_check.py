#!/usr/bin/env python3
"""ui_render_check -- gh#809 first slice: give the fleet a pre-merge look at its own UI.

WHAT THIS IS. Before this, nothing in the fleet ever rendered a page: judge-judy reads diffs as
text (`tools: none`, by design), red and sentry both run post-merge against live prod. This
script is the missing pre-merge step: for a PR that touches a template/static/route/CSS file,
boot this repo's OWN console (`scripts/fleet_view_server.py`, which serves `fleet_home.html`/
`fleet_view.html` -- the only two rendered surfaces this repo has, per gh#645/gh#573) locally,
screenshot the affected page at phone width, and list any uncaught console errors.

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
blocks a merge (gh#809's own second UNKNOWN), and desktop-viewport rendering (the issue's `Fix`
section mentions it, but no acceptance criterion requires it -- phone-width only, criterion 1).
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
VIEWPORT = {"width": 390, "height": 844}


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


def boot_server(log_dir: Path, port: int, timeout: float = 25.0) -> subprocess.Popen:
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


def render_surface(url: str, out_png: Path) -> dict:
    from playwright.sync_api import sync_playwright  # local import: keep this optional for
    # callers (and test_ui_render_check.py) that only need select_surfaces()'s pure logic.

    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport=VIEWPORT)
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.goto(url, timeout=30000, wait_until="load")
            page.wait_for_timeout(500)
            text = page.inner_text("body") or ""
            out_png.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_png))
        finally:
            browser.close()
    return {"console_errors": console_errors, "text_len": len(text.strip())}


def _write_report(out_dir: Path, report: dict) -> None:
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
        for path, s in sorted(report["surfaces"].items()):
            lines.append(f"### `{path}` ({s['name']})")
            if s.get("error"):
                lines.append(f"- render error: {s['error']}")
            else:
                lines.append(f"- screenshot (390x844): `{s['screenshot']}`")
                if s.get("blank"):
                    lines.append(
                        f"- **BLANK RENDER** -- only {s['text_len']} chars of body text "
                        f"(threshold {BLANK_TEXT_THRESHOLD})"
                    )
                errs = s.get("console_errors") or []
                if errs:
                    lines.append(f"- console errors ({len(errs)}):")
                    for e in errs[:20]:
                        lines.append(f"  - `{e}`")
                else:
                    lines.append("- console errors: none")
            lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="git ref to diff HEAD against")
    ap.add_argument("--out", default="qa-out/ui_render", help="output directory for report + screenshots")
    ap.add_argument("--changed-files", nargs="*", default=None,
                     help="override the changed-file list instead of computing it from --base")
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
        _write_report(out_dir, report)
        print(f"ui_render_check: UNAVAILABLE -- {exc}")
        return 0

    try:
        for path, name in sorted(surfaces.items()):
            url = f"http://127.0.0.1:{port}{path}"
            png = out_dir / f"{name}_390x844.png"
            try:
                r = render_surface(url, png)
                blank = r["text_len"] < BLANK_TEXT_THRESHOLD
                report["surfaces"][path] = {
                    "name": name, "screenshot": str(png),
                    "console_errors": r["console_errors"],
                    "text_len": r["text_len"], "blank": blank,
                }
                if blank:
                    print(f"ui_render_check: BLANK RENDER at {path} ({r['text_len']} chars)")
            except Exception as exc:  # noqa: BLE001
                report["surfaces"][path] = {"name": name, "error": str(exc)}
    finally:
        proc.kill()
        proc.wait(timeout=10)

    _write_report(out_dir, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
