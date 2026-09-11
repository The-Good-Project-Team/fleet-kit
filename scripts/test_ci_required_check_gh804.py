#!/usr/bin/env python3
"""gh#804: the required `selftest` check must never depend on an external apt mirror.

THE BUG. `.github/workflows/ci.yml`'s `selftest` job -- the only required status check
(branch protection + auto-merge key on that name, see docs/ops/fleet-kit-governance.md) --
used to run `playwright install --with-deps chromium`. `--with-deps` shells out to `apt-get`
against `packages.microsoft.com` on every run, cache hit or not. On 2026-09-09 that mirror
returned 403, and PR #801 -- a one-file, non-Playwright change -- was ejected from the merge
queue. The fix moves the apt-dependent step out of the required job into a separate,
non-required `playwright-tests` job that still runs (and is still visible) on every PR.

RED without the fix: test_required_job_has_no_with_deps_step fails against `main`, because
`selftest` still contains a `--with-deps` step.

Plain-python test, no pytest -- matches ci.yml, which runs `python3 scripts/test_*.py`.
Parses ci.yml with a plain regex rather than requiring PyYAML, so this test itself never
depends on anything beyond the stdlib.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


def _job_block(ci_text: str, name: str) -> str:
    # job keys sit at 2-space indent directly under `jobs:`; a job's own steps are indented
    # further, so the next 2-space-indented key (or end of file) closes its block.
    job_starts = list(re.finditer(r"^  ([a-zA-Z0-9_-]+):[ \t]*$", ci_text, re.MULTILINE))
    names = [m.group(1) for m in job_starts]
    assert name in names, f"job {name!r} not found in ci.yml (jobs seen: {names})"
    idx = names.index(name)
    start = job_starts[idx].start()
    end = job_starts[idx + 1].start() if idx + 1 < len(job_starts) else len(ci_text)
    block = ci_text[start:end]
    # drop comment-only lines: a comment may legitimately name `--with-deps` (e.g. explaining
    # why a later, non-required job is allowed to run it) without that being a real step here
    return "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))


def test_required_job_has_no_with_deps_step() -> None:
    ci_text = CI_YML.read_text()
    required = os.environ.get("FLEET_REQUIRED_CHECKS", "").split() or ["selftest"]
    for name in required:
        block = _job_block(ci_text, name)
        assert "--with-deps" not in block, (
            f"required job {name!r} still runs `--with-deps`, which shells out to an "
            "external apt mirror -- an outage there fails the check even though "
            "selftest.py itself passed (gh#804)"
        )
    print(f"ok  required check(s) {required} run no --with-deps step")


def test_playwright_dependent_tests_still_run_every_pr() -> None:
    ci_text = CI_YML.read_text()
    # test_settings_wiring.py imports playwright at module level -- it must still run on
    # every PR (AC4), just not gate the merge queue on an apt mirror.
    assert "run: python3 scripts/test_settings_wiring.py" in ci_text, (
        "test_settings_wiring.py must still run on every PR, just outside the required job"
    )
    playwright_block = _job_block(ci_text, "playwright-tests")
    assert "test_settings_wiring.py" in playwright_block, (
        "test_settings_wiring.py must live in the (non-required) playwright-tests job"
    )
    selftest_block = _job_block(ci_text, "selftest")
    assert "test_settings_wiring.py" not in selftest_block, (
        "test_settings_wiring.py must not run inside the required selftest job"
    )


def main() -> int:
    try:
        test_required_job_has_no_with_deps_step()
        test_playwright_dependent_tests_still_run_every_pr()
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
