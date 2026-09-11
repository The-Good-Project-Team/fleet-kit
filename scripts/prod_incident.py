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

gh#836: the marker alone answers "is ANY incident open", never "is an incident open for THIS
target" -- so two distinct, concurrently-open outages (e.g. two different failing URLs) used to
collapse onto one issue, silently hiding the second. `find_open_incident`/
`file_or_update_incident` now take an optional `target` that narrows the match to an issue
recorded with that same target; a caller that omits it (or an issue filed before this change)
keeps the old "first marked issue, any target" behaviour so gh#728 fix 3's cross-member race
dedup is unaffected. See `find_open_incident` and `_extract_target` for the exact rule.
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
# gh#836: additional, optional discriminator recording WHICH outage this issue is about (a URL,
# today). Kept separate from INCIDENT_MARKER itself (that marker's only job stays "this is a
# machine-filed incident issue at all") so a caller with no discriminator to supply still files
# and dedupes exactly as before.
_TARGET_MARKER_PREFIX = "<!-- fleet-prod-incident-target:"
_TARGET_MARKER_SUFFIX = " -->"
_TARGET_MARKER_RE = re.compile(re.escape(_TARGET_MARKER_PREFIX) + r"(.*?)" + re.escape(_TARGET_MARKER_SUFFIX))


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


def _target_marker(target: str) -> str:
    return f"{_TARGET_MARKER_PREFIX}{target}{_TARGET_MARKER_SUFFIX}"


def _extract_target(body: str) -> str | None:
    """None means no discriminator was ever recorded on this issue -- either it predates gh#836,
    or its filer (fixer_fire_path.py today -- see that module's header) has no per-target
    identifier to pass. Such an issue is treated as matching ANY target search rather than none
    (see find_open_incident): gh#728 fix 3's cross-member race dedup relies on exactly that --
    one filer supplying a target and the other not, for what is in production the same single
    outage. Narrowing this to "no match" would silently break that dedup, which gh#836's PRD
    lists as a non-goal to preserve."""
    m = _TARGET_MARKER_RE.search(body)
    return m.group(1) if m else None


def build_create_cmd(repo: str, title: str, body: str, labels: list[str],
                      target: str = "") -> list[str]:
    marker = INCIDENT_MARKER
    if target:
        marker = f"{marker}\n{_target_marker(target)}"
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title,
           "--body", f"{body}\n\n{marker}"]
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


def find_open_incident(repo: str, target: str = "", run=_run) -> int | None:
    """Which open, marker-carrying issue (if any) a filing for `target` should attach to.

    No `target` given (gh#836's AC6/AC7 legacy case, and today's fixer_fire_path.py -- it has no
    per-target identifier to pass): behaves exactly as before gh#836, first marked issue, in
    whatever order `gh` returns.

    `target` given: matches only an issue recorded with that SAME target (gh#836 AC1/AC2/AC5),
    so two concurrently-open incidents for two different targets never collapse onto one. An
    issue with NO recorded target (filed before gh#836, or by a caller with no discriminator)
    still matches any target -- chosen deliberately, see _extract_target's docstring."""
    marked = _list_marked_issues(repo, run=run)
    if not marked:
        return None
    if not target:
        return marked[0]["number"]
    for issue in marked:
        if _extract_target(issue.get("body") or "") == target:
            return issue["number"]
    for issue in marked:
        if _extract_target(issue.get("body") or "") is None:
            return issue["number"]
    return None


def find_all_open_incidents(repo: str, run=_run) -> list[int]:
    return [i["number"] for i in _list_marked_issues(repo, run=run)]


def file_or_update_incident(repo: str, title: str, body: str, labels: list[str],
                             run=_run, target: str = "") -> tuple[int | None, bool]:
    """Returns (issue_number, created). Whichever prod-down filer runs first wins the issue;
    the other one finds it by INCIDENT_MARKER (and, when both sides supply the same `target`,
    by target too) and comments instead of creating a second one (gh#728 VP fix 3, narrowed by
    gh#836 so two DIFFERENT targets no longer collapse onto one issue)."""
    number = find_open_incident(repo, target=target, run=run)
    if number is not None:
        run(build_comment_cmd(repo, number, body))
        return number, False

    rc, out = run(build_create_cmd(repo, title, body, labels, target=target))
    if rc != 0:
        return None, False
    m = re.search(r"/issues/(\d+)\s*$", out)
    return (int(m.group(1)) if m else None), True
