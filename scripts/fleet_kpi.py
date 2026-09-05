#!/usr/bin/env python3
"""fleet_kpi — extract each member's own headline count from its run's `outcome` prose.

WHY PER-MEMBER PATTERNS, NOT ONE GENERIC "find a number" REGEX: tried the generic version first
against real runs.jsonl outcomes (roomba/marie samples, 2026-08-25) -- it matches "0 removed"
and "0 errors" as readily as a real achievement count, because outcome prose is free text, not a
report format. A member's KPI is domain knowledge (what does THIS member's job actually count?),
not a string-shape you can infer generically. See each pattern's own comment for the real
outcome strings it was built against.

gh#230: gru/jefe/minion DO have a count-shaped headline after all -- "PRs shipped," read off
the same `PR #<n>` / `pull/<n>` mentions their own outcome prose already names -- so they're in
`_KPI_TABLE` below like everyone else. Members that still get no pattern and correctly return
None are the-fixer (its own PR-mention hit rate doesn't fit a "shipped" headline, see gh#230's
non-goals) and dumbledore/dont-shoot-the-messenger (poll-and-report, nothing to count). There is
no pass/fail-ratio dashboard fallback for those -- none exists anywhere in this kit (checked:
neither `fleet_view.html` nor `fleet_view_server.py` render one) -- they simply have no KPI slot
today.
"""
from __future__ import annotations

import re

# Each pattern extracts every "N <the counted thing>" occurrence relevant to that member's own
# job and SUMS them across one outcome string -- a single pass often reports multiple countable
# actions ("cleared 4 stale claims... closed 2 cruft issues... labeled 3 issues" is 9 actions
# taken, not 4). unit is a fixed human label, not derived from the matched text, so the total
# always reads as one coherent thing regardless of which clause it came from.

# roomba's real KPI is its worktree sweep count -- always present, every pass ("Worktree sweep
# clean (5 evaluated/0 removed...)", "3 worktrees evaluated / 0 removed"). "evaluated" doesn't
# always sit directly next to the word "worktree" (the sweep-summary sentence separates them),
# so one pattern anchors on "N evaluated" alone. gh#343: that bare anchor missed ~30-40% of real
# sweeps because roomba just as often writes the verb *before* the number ("evaluated 1
# worktree") or slots the word "worktree" between the digit and "evaluated" ("1 worktree
# evaluated") -- three real phrasings, three patterns, still all feeding the same one unit.
# ONLY these patterns feed the KPI -- ghosts found and worktrees pruned are real but SEPARATE
# metrics with their own units; blending them into one summed number under whichever unit
# matched last (an earlier version of this file did exactly that) produces a total that's
# technically-a-number but means nothing (e.g. "46 ghosts found" when only 9 of 21 runs even
# mentioned a ghost, because worktree-evaluated counts got silently added in under the ghost
# label). One pattern family, one meaning, per member.
_ROOMBA_PATTERNS = [
    (re.compile(r"(\d+)\s+worktrees?\s+evaluated", re.I), "worktrees evaluated"),
    (re.compile(r"evaluated\s+(\d+)\s+worktrees?", re.I), "worktrees evaluated"),
    (re.compile(r"(\d+)\s+evaluated", re.I), "worktrees evaluated"),
]

# marie's real outcome prose (2026-08-25 sample, 17 runs) is far more varied than "cleared N
# stale claims" -- half her count-shaped passes name specific issues ("Cleared stale
# fleet:claimed on #2075 and #2759", "Closed #2217 as cruft") with no leading digit at all.
# Two families of pattern: an explicit "N <thing>" count where one exists, PLUS an
# issue-reference counter (count how many #NNNN's appear after a triage verb) for the
# common case where marie names issues instead of counting them. The issue-reference family
# only fires on triage VERBS, never on issues merely mentioned for context (e.g. "confirmed 65
# open issues", "5 newly-merged PRs") -- those aren't marie's own actions taken.
#
# gh#351: the original verb list (cleared/closed/ranked) missed the rest of marie's actual
# triage vocabulary -- "triaged", "corrected priority on", "backfilled complexity on", "bumped
# ... complexity", "scored complexity on", "wrote/posted a PRD for", "labeled" -- silently
# ZERO-counting those runs (same failure class as gh#343's `_ROOMBA_PATTERNS` gap, one file
# over). Also missing: the explicit-count phrasing "N new ranking(s)", which named no issue
# numbers at all so neither existing family could ever catch it.
_MARIE_PATTERNS = [
    (re.compile(r"(\d+)\s*(?:×|x)\s*`?gh issue", re.I), "issues triaged"),
    (re.compile(r"(\d+)\s+(?:previously-unranked|priority-labeled|new issues?)", re.I), "issues triaged"),
    (re.compile(r"(\d+)\s+new\s+rankings?", re.I), "issues triaged"),
    # verb-anchored issue-number counting: "cleared stale ... on #A and #B" -> 2, "closed #X as
    # cruft" -> 1, "ranked #A ... and #B" -> 2, "triaged #A", "corrected priority on #A",
    # "backfilled complexity on #A and #B", "bumped #A's complexity", "scored complexity on
    # #A", "wrote/posted a PRD for #A", "labeled #A". Anchored to the verb + the reference LIST
    # that immediately follows it (stops at the next `;`/`.` clause boundary), so it never
    # counts issue numbers mentioned later in the sentence for unrelated context.
    (re.compile(
        r"\b(?:cleared|closed|ranked|triaged|corrected|backfilled|bumped|scored|wrote|posted|"
        r"labeled)\b[^;.]*?((?:#\d+\D{0,6}){1,10})",
        re.I,
    ), "issues triaged"),
]


def _count_issue_refs(fragment: str) -> int:
    return len(re.findall(r"#\d+", fragment))

_JUDGE_JUDY_PATTERNS = [
    (re.compile(r"approved PR #\d+"), "PRs reviewed"),
    (re.compile(r"(?:blocked|rejected) PR #\d+"), "PRs reviewed"),
]

# gru/jefe/minion's real outcome prose is heavily PR-shaped ("Shipped PR #226 ... and PR #227
# ... both auto-merge armed") -- one occurrence per PR mention, not the PR NUMBER itself, so
# (unlike roomba/marie's explicit "N <thing>" patterns) these carry no capturing group: each
# match of the whole pattern is one shipped PR, same shape as `_JUDGE_JUDY_PATTERNS` above.
_PR_SHIPPED_PATTERNS = [
    (re.compile(r"PR #\d+", re.I), "PRs shipped"),
    (re.compile(r"pull/\d+", re.I), "PRs shipped"),
]

# member name -> (pattern list, fallback unit label if any pattern matches with no explicit unit)
_KPI_TABLE: dict[str, list[tuple[re.Pattern, str]]] = {
    "roomba": _ROOMBA_PATTERNS,
    "marie": _MARIE_PATTERNS,
    "judge-judy": _JUDGE_JUDY_PATTERNS,
    "gru": _PR_SHIPPED_PATTERNS,
    "jefe": _PR_SHIPPED_PATTERNS,
    "minion": _PR_SHIPPED_PATTERNS,
}

# gh#230 AC3: these three must report a real (0, "PRs shipped") -- not None -- on a pass that
# shipped nothing, since a build-loop run genuinely CAN complete with no PR (blocked, already
# fixed, QUIET). roomba/marie/judge-judy don't need this: roomba's "N evaluated" phrasing is
# present on every one of its passes by convention, and marie/judge-judy's None-on-no-match is
# their own already-tested, intentional behavior (a pass that named no issue/PR really did
# nothing triage/review-shaped) -- not something this issue's scope touches.
_REAL_ZERO_MEMBERS = {"gru", "jefe", "minion"}


def extract_kpi(member: str, outcome: str | None) -> tuple[int, str] | None:
    """(count, unit_label) summed across every matching clause in this one run's outcome, or
    None if this member has no defined KPI shape or the outcome didn't match anything (a QUIET
    run that did no countable work is a real zero, still returned as (0, unit) if the member
    HAS a KPI shape -- e.g. roomba's "0 worktrees evaluated" -- but a member with no shape at
    all, or an outcome of None (budget_declined/crashed runs carry outcome=null), is a true
    None: there is nothing to sum, not a zero."""
    if outcome is None or member not in _KPI_TABLE:
        return None
    patterns = _KPI_TABLE[member]
    total = 0
    unit = None
    matched_any_pattern = False
    for pat, label in patterns:
        for m in pat.finditer(outcome):
            matched_any_pattern = True
            # Three capture shapes coexist: no group at all (judge-judy -- the match itself IS
            # one occurrence), a group that's a literal digit string (roomba/marie's explicit
            # "N <thing>" patterns), or a group that's a text fragment to count #issue
            # references WITHIN (marie's verb-anchored pattern, whose captured group is
            # "#2075 and #2759 ", not a number). isdigit() tells these apart cheaply.
            if not m.groups():
                total += 1
            else:
                g = m.group(1)
                total += int(g) if g.isdigit() else max(1, _count_issue_refs(g))
            unit = label
    if not matched_any_pattern:
        if member in _REAL_ZERO_MEMBERS:
            return (0, patterns[0][1])
        return None
    return (total, unit or "")


def sum_kpi_over_runs(member: str, runs: list[dict]) -> dict:
    """Aggregate extract_kpi across a window of this member's own runs.jsonl records --
    the shape /api/kpi returns per member: total achieved, unit label, and how many of the
    window's runs actually had a countable outcome vs. ran but reported nothing count-shaped
    (a QUIET/budget_declined run is real, just not counted -- runs_total stays honest even
    when runs_with_kpi is 0, so the dashboard never implies "this member did nothing" when it
    just means "nothing here was the countable kind").
    """
    total = 0
    unit = ""
    runs_with_kpi = 0
    for r in runs:
        if r.get("member") != member:
            continue
        got = extract_kpi(member, r.get("outcome"))
        if got is not None:
            n, u = got
            total += n
            unit = u or unit
            runs_with_kpi += 1
    runs_total = sum(1 for r in runs if r.get("member") == member)
    return {
        "member": member,
        "total": total if member in _KPI_TABLE else None,
        "unit": unit,
        "runs_with_kpi": runs_with_kpi,
        "runs_total": runs_total,
    }
