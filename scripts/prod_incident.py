#!/usr/bin/env python3
"""prod_incident.py -- ONE shared incident marker + filing/dedup helper for the fleet's two
prod-down filers: prod_health_check.py (gh#727, external HTTP/heartbeat detection) and
fixer_fire_path.py (gh#728, the-fixer's unattended rollback response).

WHY THIS EXISTS (gh#728 VP follow-up, round 2, fix 3): each filer used to dedupe against its
OWN key -- prod_health_check.py by exact issue TITLE ("prod down: <url>"), fixer_fire_path.py
by a hidden marker only it ever wrote into a DIFFERENT title ("PROD DOWN -- automatic
rollback ..."). Neither could ever find the other's issue, so one real "philanthropy.org is
unreachable" outage produced two open incident tickets, one per filer. The fix is not a
smarter search on either side -- it's the same body marker, the same lookup, and the same
target repo, called from both, with the repo passed explicitly (never inferred from the
caller's cwd, which is how fixer_fire_path.py's old command silently depended on `FLEET_REPO`
having been `cd`'d into first).

gh's own `--search` flag full-text-tokenizes a marker and can match unrelated issues that
merely contain some of its words -- verified live against this repo: it returned issues that
never contained the literal marker at all. Identity here is therefore never `--search`: it is
an exact substring test, in Python, against the BODY of every OPEN issue carrying the
`incident` label -- the same convention prod_health_check.py's own pre-existing title check
already used, just applied to the marker instead of a title string.

Only the "a probe/health-url is actually unreachable" class of incident goes through this
shared path (see prod_health_check.py's `file_or_update_incident` for which branch that is) --
a signal only one member watches (like the app_error_canary heartbeat) has no reason to risk
being silently absorbed into an unrelated ticket just because both happen to use this module.
"""
from __future__ import annotations

import json
import re
import subprocess

INCIDENT_LABEL = "incident"
INCIDENT_REPO = "The-Good-Project-Team/philanthropy"
# Shared across both filers (gh#728 VP fix 3) -- whichever one notices an outage first, the
# other's search finds ITS issue by this marker and comments rather than filing a second one.
INCIDENT_MARKER = "<!-- fleet-prod-incident -->"


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def build_list_open_incidents_cmd(repo: str) -> list[str]:
    # No --search: gh's full-text search tokenizes the marker and can match issues that never
    # contained it. Identity is decided in Python against each candidate's body (see
    # find_open_incident/find_all_open_incidents below).
    return ["gh", "issue", "list", "--repo", repo, "--state", "open", "--label", INCIDENT_LABEL,
            "--json", "number,body", "--limit", "50"]


def build_create_cmd(repo: str, title: str, body: str, labels: list[str]) -> list[str]:
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title,
           "--body", f"{body}\n\n{INCIDENT_MARKER}"]
    for label in labels:
        cmd += ["--label", label]
    return cmd


def build_comment_cmd(repo: str, number: int, body: str) -> list[str]:
    return ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body]


def _list_marked_issues(repo: str, run=_run) -> list[dict]:
    rc, out = run(build_list_open_incidents_cmd(repo))
    if rc != 0 or not out:
        return []
    try:
        issues = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [i for i in issues if INCIDENT_MARKER in (i.get("body") or "")]


def find_open_incident(repo: str, run=_run) -> int | None:
    """First open issue (in whatever order `gh` returns) whose body carries the marker."""
    marked = _list_marked_issues(repo, run=run)
    return marked[0]["number"] if marked else None


def find_all_open_incidents(repo: str, run=_run) -> list[int]:
    return [i["number"] for i in _list_marked_issues(repo, run=run)]


def file_or_update_incident(repo: str, title: str, body: str, labels: list[str],
                             run=_run) -> tuple[int | None, bool]:
    """Returns (issue_number, created). Whichever prod-down filer runs first wins the issue;
    the other one finds it by INCIDENT_MARKER and comments instead of creating a second one
    (gh#728 VP fix 3)."""
    number = find_open_incident(repo, run=run)
    if number is not None:
        run(build_comment_cmd(repo, number, body))
        return number, False

    rc, out = run(build_create_cmd(repo, title, body, labels))
    if rc != 0:
        return None, False
    m = re.search(r"/issues/(\d+)\s*$", out)
    return (int(m.group(1)) if m else None), True
