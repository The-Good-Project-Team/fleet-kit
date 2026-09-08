#!/usr/bin/env python3
"""fixer_fire_path.py -- the-fixer's unattended PROD DOWN response: diagnose, roll back,
verify, record.

Provenance: The-Good-Project-Team/philanthropy#4902 (Reif-priority parent), fleet-kit gh#727
(detection half) and gh#728 (this file, the response half). check.sh already gives the-fixer
eyes on prod (FIXER_HEALTH_URL/FIXER_PAGE_URL) and a diagnosis (FIXER_PROD_DIAG_DRIVER,
gh#4546) -- this is the third and last piece: act on what it saw, and PROVE the action worked
before ever reporting success.

CONTRACT (same shape as board_github.py): pure command builders, unit-tested without ever
touching `gh`/`ssh`/a real deploy driver, a thin `_run` execution wrapper, and run_fire_path()
as the one orchestrator a real caller (the-fixer's charter, Step 2, on a `FIRE PROD DOWN` line
from check.sh) invokes with real driver paths.

THE ONE RULE THIS FILE EXISTS TO ENFORCE (marie's PRD on gh#728, AC2 -- "the single most
important criterion in this issue"): a deploy driver whose `rollback` silently no-ops must
NEVER be reported as a successful rollback. `current_sha` is read BEFORE and AFTER rollback and
the two are compared; a driver merely exiting 0 is not proof by itself -- a rollback that
silently no-ops while reporting success is worse than no rollback at all.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

PREFIX = os.environ.get("FLEET_LABEL_PREFIX", "fleet:")
INCIDENT_LABEL = "incident"
PRIORITY_HIGH_LABEL = f"{PREFIX}priority-high"
# A fixed, hidden marker every incident this file opens carries, so a repeat firing can find and
# update the SAME issue (AC7) -- the fire's own dedup key in check.sh (a 30-minute "prod-<N>"
# time bucket) rotates every half hour and can't be used to find yesterday's still-open incident.
INCIDENT_MARKER = "<!-- fixer-fire-path-incident -->"


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


# --- Step 1: diagnose, always before any rollback attempt (AC1) -------------------------------

def build_diag_cmd(driver: str, section: str) -> list[str]:
    return [driver, section]


def diagnose(driver: str, section: str, run=_run) -> tuple[bool, str]:
    """Runs the diag driver read-only. Returns (ok, output) -- ok is False only if the driver
    itself failed to execute, never a judgment call on what the output says (same convention as
    check.sh's own FIXER_PROD_DIAG_DRIVER invocation)."""
    rc, out = run(build_diag_cmd(driver, section))
    return rc == 0, out


# --- Step 2: roll back, then PROVE it (AC2/AC3) ------------------------------------------------

@dataclass
class RollbackResult:
    verified: bool
    old_sha: str
    new_sha: str
    message: str


def build_current_sha_cmd(driver: str) -> list[str]:
    return [driver, "current_sha"]


def build_rollback_cmd(driver: str) -> list[str]:
    return [driver, "rollback"]


def roll_back_and_verify(driver: str, run=_run) -> RollbackResult:
    """Never trusts `rollback`'s own exit code alone -- reads current_sha before and after and
    only calls it verified when the two differ. A driver that exits 0 but leaves current_sha
    unchanged reads as ROLLBACK FAILED, not success (AC2)."""
    old_rc, old_sha = run(build_current_sha_cmd(driver))
    if old_rc != 0 or not old_sha:
        return RollbackResult(False, "", "", "ROLLBACK FAILED -- current_sha unreadable before rollback; refusing to roll back blind")

    rb_rc, rb_out = run(build_rollback_cmd(driver))

    new_rc, new_sha = run(build_current_sha_cmd(driver))
    if new_rc != 0 or not new_sha:
        return RollbackResult(False, old_sha, "", "ROLLBACK FAILED -- current_sha unreadable after rollback")

    if rb_rc != 0:
        return RollbackResult(False, old_sha, new_sha, f"ROLLBACK FAILED -- rollback exited {rb_rc}: {rb_out}"[:500])

    if new_sha == old_sha:
        return RollbackResult(False, old_sha, new_sha,
                               "ROLLBACK FAILED -- current_sha unchanged after rollback (driver no-op)")

    return RollbackResult(True, old_sha, new_sha, f"rolled back {old_sha[:12]} -> {new_sha[:12]}")


# --- post-promote breach detection (AC4) -------------------------------------------------------

def _breaches(sample: dict, error_pct_threshold: float, p95_threshold_s: float) -> bool:
    return sample.get("error_pct", 0) > error_pct_threshold or sample.get("p95_s", 0) > p95_threshold_s


def should_fire_post_promote(samples: list[dict], error_pct_threshold: float = 5.0,
                              p95_threshold_s: float = 6.0) -> bool:
    """Fires only on two CONSECUTIVE breaching samples in the given order -- one bad sample is a
    blip, never an outage (same double-probe reasoning check.sh already applies to
    FIXER_HEALTH_URL/FIXER_PAGE_URL). A breach, then a clean sample, then another breach never
    fires -- the two breaches must be adjacent."""
    prev_breach = False
    for sample in samples:
        breach = _breaches(sample, error_pct_threshold, p95_threshold_s)
        if breach and prev_breach:
            return True
        prev_breach = breach
    return False


# --- Step 3: file or update the incident, never duplicate it (AC7) -----------------------------

def build_incident_search_cmd() -> list[str]:
    # No --repo flag: same convention board_github.py already uses -- the caller `cd`s into the
    # product repo (FLEET_REPO) first, and `gh` infers the repo from the working directory's
    # git remote, so this works unmodified for whatever repo an instance is pointed at.
    return ["gh", "issue", "list", "--state", "open", "--label", INCIDENT_LABEL,
            "--search", INCIDENT_MARKER, "--json", "number", "--limit", "5"]


def build_incident_create_cmd(title: str, body: str) -> list[str]:
    return ["gh", "issue", "create", "--title", title, "--body", f"{body}\n\n{INCIDENT_MARKER}",
            "--label", f"{PRIORITY_HIGH_LABEL},{INCIDENT_LABEL}"]


def build_incident_comment_cmd(number: int, body: str) -> list[str]:
    return ["gh", "issue", "comment", str(number), "--body", body]


def find_open_incident(run=_run) -> int | None:
    rc, out = run(build_incident_search_cmd())
    if rc != 0 or not out:
        return None
    try:
        found = json.loads(out)
    except json.JSONDecodeError:
        return None
    return found[0]["number"] if found else None


def file_or_update_incident(title: str, body: str, run=_run) -> tuple[int | None, bool]:
    """Returns (issue_number, created). A repeat firing updates the SAME open incident rather
    than filing a second one (AC7) -- searched by INCIDENT_MARKER."""
    number = find_open_incident(run=run)
    if number is not None:
        run(build_incident_comment_cmd(number, body))
        return number, False

    rc, out = run(build_incident_create_cmd(title, body))
    if rc != 0:
        print(f"fixer_fire_path: filing the incident FAILED: {out[:300]}", file=sys.stderr)
        return None, False
    m = re.search(r"/issues/(\d+)\s*$", out)
    return (int(m.group(1)) if m else None), True


# --- orchestrator --------------------------------------------------------------------------

def run_fire_path(*, diag_driver: str, diag_section: str, deploy_driver: str, run=_run) -> dict:
    """The whole unattended sequence: diagnose (AC1) -> roll back + verify (AC2/AC3) -> file or
    update the incident with both shas and the diag output (AC3/AC7). Diagnose-then-act ordering
    is enforced by call order below, not by a flag a caller could skip."""
    diag_ok, diag_output = diagnose(diag_driver, diag_section, run=run)
    rollback = roll_back_and_verify(deploy_driver, run=run)

    title = f"PROD DOWN -- automatic rollback {'succeeded' if rollback.verified else 'FAILED'}"
    body = "\n".join([
        f"the-fixer fired automatically. Rollback: {'SUCCESS' if rollback.verified else 'ROLLBACK FAILED'}.",
        f"old_sha: {rollback.old_sha or '(unreadable)'}",
        f"new_sha: {rollback.new_sha or '(unreadable)'}",
        rollback.message,
        "",
        "## Diagnosis" + ("" if diag_ok else " (driver failed to run -- output below may be partial)"),
        "```",
        diag_output or "(no diag output)",
        "```",
    ])

    issue_number, created = file_or_update_incident(title, body, run=run)

    return {
        "diag_ok": diag_ok,
        "diag_output": diag_output,
        "rollback_verified": rollback.verified,
        "old_sha": rollback.old_sha,
        "new_sha": rollback.new_sha,
        "message": rollback.message,
        "issue_number": issue_number,
        "issue_created": created,
    }


if __name__ == "__main__":
    diag_driver = os.environ.get("FIXER_PROD_DIAG_DRIVER", "")
    deploy_driver = os.environ.get("FLEET_DEPLOY_DRIVER", "")
    repo = os.environ.get("FLEET_REPO", "")
    if not diag_driver or not deploy_driver:
        print("fixer_fire_path: FIXER_PROD_DIAG_DRIVER and FLEET_DEPLOY_DRIVER must both be set "
              "-- refusing to roll back blind", file=sys.stderr)
        sys.exit(1)
    if repo:
        os.chdir(repo)  # `gh` infers owner/repo from the cwd's git remote (board_github.py convention)
    result = run_fire_path(
        diag_driver=diag_driver,
        diag_section=os.environ.get("FIXER_PROD_DIAG_SECTION", "pg"),
        deploy_driver=deploy_driver,
    )
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["rollback_verified"] else 1)
