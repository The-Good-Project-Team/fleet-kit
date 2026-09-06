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
    #
    # gh#409: marie's QUIET-pass boilerplate routinely writes explicit-zero counts right before
    # the same verb -- "0 claims cleared (gh#98 self-resolved via merged PR#404)", "0 stale
    # claims cleared" -- where the verb legitimately fires and an unrelated #NNNN reference
    # (named for context, not as a counted action) sits in the clause that follows. The optional
    # leading group below captures that "0 <1-3 word noun phrase> " prefix when present,
    # immediately before the verb; a match carrying it is skipped entirely in extract_kpi()
    # rather than credited. The `(?!:)` guard after the verb covers the sibling shape "...is
    # fully triaged: 0 stale claims, ..." -- a label/summary use of the verb, not an action on
    # what follows it -- so the same non-greedy issue-ref scan can't reach across the colon into
    # an unrelated later "0 ..." clause that happens to name an issue for context. The `(?<!-)`
    # guard before the verb excludes compound-modified verbs like "auto-closed"/"self-closed" --
    # an automated/self-resolved outcome, not marie's own action -- from firing the same verb.
    (re.compile(
        r"(\b0\s+(?:\S+\s+){1,3})?"
        r"(?<!-)\b(?:cleared|closed|ranked|triaged|corrected|backfilled|bumped|scored|wrote|posted|"
        r"labeled)\b(?!:)[^;.]*?((?:#\d+\D{0,6}){1,10})",
        re.I,
    ), "issues triaged"),
]


def _count_issue_refs(fragment: str) -> int:
    # "#\d+" covers marie/nerd's issue references; "pull/\d+" covers `_PR_SHIPPED_PATTERNS`
    # below (a "PR #\d+" ref is already caught by "#\d+", but a bare "pull/4488" ref has no
    # "#" at all -- without this second alternative a clause naming two "pull/N" PRs and no
    # "PR #N" ones would undercount to 1 via extract_kpi's `max(1, ...)` floor).
    return len(re.findall(r"#\d+|pull/\d+", fragment))


def _count_pr_refs(fragment: str) -> int:
    """gh#448: `_PR_SHIPPED_PATTERNS`'s merged capture can interleave real PR refs with
    unrelated "gh#N" issue-cause mentions in the SAME clause ("gh#196->PR#199, gh#194->PR#200"
    -- gh#196/gh#194 are the issues each PR closes, not shipped PRs themselves). A bare
    `_count_issue_refs`-style "#\\d+" scan over the whole fragment would credit those issue
    numbers too. Scan for `_PR_REF` *cluster* matches first (each one anchored on an actual
    "PR"/"PRs"/"pull/" keyword), then count "#\\d+" only within each cluster -- a stray "gh#194"
    sitting in the gap BETWEEN two clusters is never part of either match, so it's never
    counted."""
    return sum(
        len(re.findall(r"#\d+|pull/\d+", cluster.group()))
        for cluster in re.finditer(_PR_REF, fragment, re.I)
    )

_JUDGE_JUDY_PATTERNS = [
    (re.compile(r"approved PR #\d+"), "PRs reviewed"),
    (re.compile(r"(?:blocked|rejected) PR #\d+"), "PRs reviewed"),
]

# gh#230: gru/jefe/minion's real outcome prose is heavily PR-shaped ("Shipped PR #226 ... and
# PR #227 ... both auto-merge armed"). Anchored to an actual shipping VERB
# (shipped/opened/merged/armed) immediately before the PR reference(s), same shape as
# `_JUDGE_JUDY_PATTERNS`'s own verb anchor -- an EARLIER version of this matched bare
# "PR #\d+"/"pull/\d+" with no verb at all, so a plausible outcome like "Attempted PR #225 but
# CI failed, blocked. Retried and shipped PR #226." counted BOTH PRs as shipped when only #226
# actually was (found live in fleet-code-review on this PR, 2026-09-05). The clause gap mirrors
# `_NERD_CLAUSE_GAP` below: stops at ';' or a real sentence-ending '.', so "Attempted PR #225
# ... blocked." and "Retried and shipped PR #226." are two separate clauses and the verb never
# reaches back across the boundary to credit #225.
#
# gh#448: the original `PR #\d+` alternative required a literal space, silently undercounting
# two real, common outcome-prose shapes to zero: no-space "PR#N" (33/261 real gru/jefe/minion
# outcomes in runs.jsonl) and the plural shared-prefix "PRs #A and #B" (5/261, one "PR" keyword
# governing a short list of numbers). `PRs?\s*#\d+` makes the space optional and accepts the
# plural keyword; the trailing `(?:\s*(?:,|and)\s*#\d+)*` lets a single cluster match absorb an
# immediately-following ", #N"/"and #N" continuation, so "PRs #178 and #177" matches as ONE
# cluster carrying two references instead of not matching at all (a bare "#177" with no "PR"
# keyword of its own would never match otherwise). See `_count_pr_refs` above for why counting
# these clusters needs its own function rather than reusing `_count_issue_refs`.
_PR_REF = r"(?:PRs?\s*#\d+(?:\s*(?:,|and)\s*#\d+)*|pull/\d+)"
_PR_CLAUSE_GAP = r"(?:(?!;|\.(?:\s|$)).)*?"
_PR_SHIPPED_PATTERNS = [
    (re.compile(
        r"\b(?:shipped|opened|merged|armed)\b(?!\s+(?:no|nothing)\b)"
        + _PR_CLAUSE_GAP + r"(" + _PR_REF + r"(?:[^;.]*?" + _PR_REF + r")*)",
        re.I,
    ), "PRs shipped"),
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
    "gru": _PR_SHIPPED_PATTERNS,
    "jefe": _PR_SHIPPED_PATTERNS,
    "minion": _PR_SHIPPED_PATTERNS,
    "nerd": _NERD_PATTERNS,
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
            groups = m.groups()
            # gh#409: a pattern with a leading optional "explicit-zero-count" group (currently
            # only marie's verb-anchored entry) signals a false positive when that group
            # actually matched -- e.g. "0 claims cleared (gh#98 ...)" -- so skip crediting it
            # entirely rather than counting the unrelated #issue reference that follows.
            if len(groups) > 1 and groups[0]:
                continue
            matched_any_pattern = True
            # Three capture shapes coexist: no group at all (judge-judy -- the match itself IS
            # one occurrence), a group that's a literal digit string (roomba/marie's explicit
            # "N <thing>" patterns), or a group that's a text fragment to count #issue
            # references WITHIN (marie's verb-anchored pattern, whose captured group is
            # "#2075 and #2759 ", not a number). isdigit() tells these apart cheaply.
            if not groups:
                total += 1
            else:
                g = groups[-1]
                if g.isdigit():
                    total += int(g)
                elif label == "PRs shipped":
                    # gh#448: this fragment can interleave real PR refs with unrelated "gh#N"
                    # issue-cause mentions -- _count_pr_refs (per-cluster) rather than
                    # _count_issue_refs (bare "#\d+" over the whole fragment) is required here.
                    total += max(1, _count_pr_refs(g))
                else:
                    total += max(1, _count_issue_refs(g))
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
