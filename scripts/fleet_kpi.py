#!/usr/bin/env python3
"""fleet_kpi — extract each member's own headline count from its run's `outcome` prose.

WHY PER-MEMBER PATTERNS, NOT ONE GENERIC "find a number" REGEX: tried the generic version first
against real runs.jsonl outcomes (roomba/marie samples, 2026-08-25) -- it matches "0 removed"
and "0 errors" as readily as a real achievement count, because outcome prose is free text, not a
report format. A member's KPI is domain knowledge (what does THIS member's job actually count?),
not a string-shape you can infer generically. See each pattern's own comment for the real
outcome strings it was built against.

Members whose job isn't count-shaped (the-fixer: did a fire happen y/n; jefe/gru: orchestration
verdicts; minion: one item worked or blocked; dumbledore/dont-shoot-the-messenger: poll-and-
report) get no pattern and correctly return None -- the dashboard shows a pass/fail ratio for
those instead of a fabricated number, per the same "don't fake it" reasoning `/api/spend`
already applies to zero-cost runs elsewhere in this kit.
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

# gh#225: nerd's real outcome prose (2026-09-05 sample, 237 runs.jsonl records, since the
# issue's own three quoted fragments were 6 days stale by build time) leads overwhelmingly with
# "Filed"/"Commented on"/"Posted ... to/on"/"Edited" -- all real, countable identity-integrity
# actions this pass took (a filed issue, a comment posted, a stale issue body corrected).
#
# That same prose just as often NEGATES those exact verbs to describe a QUIET pass -- "no new
# issue filed", "filed nothing new", "no new gh#143 comment posted", "already filed", "not
# re-filed/re-posted" -- while still naming OLD, already-tracked issue numbers later in the same
# sentence. A naive "verb ... nearby issue ref" match credits those QUIET runs with fake work --
# the exact gh#409 bug nerd itself found and filed against marie's patterns in this same file.
# Guarded by refusing to match when the verb is immediately preceded by "issue(s)"/
# "finding(s)"/"comment(s)"/"already"/"was"/"re-" (every negation phrasing found glues one of
# those directly onto the verb) or immediately followed by "no"/"nothing". Verified against the
# full real sample: 0 QUIET runs credited, 0 double-counts, 216 total summed across 187/237 runs.
_NERD_ISSUE_REF = r"(?:gh#\d+|#\d+|issues/\d+)"
_NERD_VERB_GUARD = (
    r"(?<!issue )(?<!issues )(?<!finding )(?<!findings )(?<!comment )(?<!comments )"
    r"(?<!already )(?<!was )(?<!re-)"
)
# Non-greedy "rest of this clause" gap that still crosses the bare '.' inside a github.com URL --
# stops only at ';' or a real sentence-ending period (one followed by whitespace or EOS).
_NERD_CLAUSE_GAP = r"(?:(?!;|\.(?:\s|$)).)*?"
_NERD_PATTERNS = [
    # explicit count: "Filed 3 issues", "filed 2 new issues" (the issue body's own 2nd quoted
    # shape) -- checked first so the verb-anchored pattern below doesn't also fire on it.
    (re.compile(r"\bfiled\s+(\d+)\s+(?:new\s+)?issues?\b", re.I), "issues filed/commented"),
    (re.compile(
        _NERD_VERB_GUARD + r"\bfiled\b(?!\s+\d+\s+(?:new\s+)?issues?\b)(?!\s+(?:no|nothing)\b)"
        + _NERD_CLAUSE_GAP + r"(" + _NERD_ISSUE_REF + r"(?:\D{0,10}" + _NERD_ISSUE_REF + r")*)",
        re.I,
    ), "issues filed/commented"),
    (re.compile(
        _NERD_VERB_GUARD + r"\b(?:commented|posted|edited)\b(?!\s+(?:no|nothing)\b)"
        + _NERD_CLAUSE_GAP + r"(" + _NERD_ISSUE_REF + r"(?:\D{0,10}" + _NERD_ISSUE_REF + r")*)",
        re.I,
    ), "issues filed/commented"),
]

# member name -> (pattern list, fallback unit label if any pattern matches with no explicit unit)
_KPI_TABLE: dict[str, list[tuple[re.Pattern, str]]] = {
    "roomba": _ROOMBA_PATTERNS,
    "marie": _MARIE_PATTERNS,
    "judge-judy": _JUDGE_JUDY_PATTERNS,
    "nerd": _NERD_PATTERNS,
}


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
