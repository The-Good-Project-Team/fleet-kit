#!/usr/bin/env python3
"""closes_gate.py -- a PR may only close an issue it actually finishes (fk#629).

Reif, 2026-09-07, on finding the Telegram-messenger issue closed COMPLETED by a 143-line
docs PR whose own body said "a measurement, not a fix": "what is the root cause that this
telegram request was marked done?" The chain was: the minion wrote `Fixes #4507` on Step 1 of
a two-step item; judge-judy reviews the diff and never reads the issue; GitHub closed the issue
the second the PR merged. "Done" meant merged. Nobody checked the ask.

This is the deterministic half of the fix (judge-judy.sh runs it before spending a review;
the LLM half is the acceptance-criteria block it hands the reviewer). It reads the PR body's
closing keywords (Closes/Fixes/Resolves #N), pulls each issue's acceptance criteria, and BLOCKS
when the PR cannot possibly be finishing the issue:

  1. the PR body says so itself ("not a fix", "Step 1 only", "does not build", "first slice",
     "follow-up PR", ...) -- a PR that calls itself partial does not get to close anything;
  2. the PR touches only docs (docs/ or *.md) while the issue carries a non-docs lane label
     (lane:ui, lane:backend, ...) -- prose cannot finish a product item;
  3. the target issue carries `fleet:epic` (fk#652, docs/quality-standard.md rule 3 --
     "jefe closes epics, not PRs"): a docs/tests-only PR can never close an epic, and any PR
     closing an epic with an unaccepted (open) child is blocked, naming each open child. An
     epic with no children discoverable (neither real GitHub sub-issues nor marie's
     `decomposed into #a, #b` comment) is left alone -- an unlinked epic must never become
     unclosable just because this gate cannot see its children.

Everything else is left to the reviewer, who now sees the issue's acceptance criteria and is
told to block unless the diff meets every one. The fix for the author is always the same one
word: write `Part of #N` and list what remains.

`--epic <n>` is the second, PR-independent check (fk#652): whether an epic itself may be
closed right now. Its `closable: true` carries three DIFFERENT meanings (fk#879 -- a bare
`true` used to conflate all three, which is what let #785 and #553 close wrongly on
2026-09-11 despite each carrying a human-written "stays open" verdict in its own thread):
  1. every child is closed AND the epic's own thread carries no unanswered stays-open marker
     -- the goal was actually met;
  2. no children were discoverable at all (no real GitHub sub-issues, no `decomposed into`
     comment) -- an unlinked epic is never blocked on epic grounds, whatever its thread says;
  3. a stays-open marker exists but is OLDER than the newest child close -- later work already
     answered it.
`reason` always says which of the three (or, for `false`, which open child or which marker)
produced the verdict -- never trust a bare `true` on its own.

Usage: closes_gate.py <pr-number> [--repo owner/name]   -> JSON on stdout, exit 1 on block
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

CLOSE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:https://github\.com/[^\s/]+/[^\s/]+/issues/)?#?(\d+)",
    re.I,
)
PARTIAL_RE = re.compile(
    r"not a fix\b|measurement, not|is not the fix|is not the rewrite|step ?1 only|step 1 of|"
    r"does not (?:build|implement|propose|fix|ship)|first slice|part 1 of|remaining work|"
    r"follow-up pr|left for a (?:later|follow)|not yet (?:built|implemented|wired)|"
    r"out of scope for this pr|scope: step 1|only step 1",
    re.I,
)
DOC_PATH_RE = re.compile(r"^(docs/|.*\.md$)")
TEST_PATH_RE = re.compile(
    r"(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$", re.I
)
AC_HEADING_RE = re.compile(r"^#{1,4}\s*acceptance criteria\b.*$", re.I | re.M)
EPIC = "fleet:epic"
DECOMPOSED_RE = re.compile(r"decomposed into\s+((?:#\d+(?:\s*,\s*|\s+and\s+)?)+)", re.I)

# fk#879: literal, documented phrases only -- no sentiment/NLP guess about a comment's mood
# (marie.md Part C0 sets the same rule for fleet:needs-retriage). "stays open" also matches
# "epic stays open"; both are quoted verbatim in #785 (2026-09-09T21:40:04Z, "this epic stays
# open") and #553 (2026-09-11T09:06:58Z, "Epic stays open and tracking-only"). "not yet (vp
# review):" is VP's own reopen verdict (used on #553 at 2026-09-11T05:53:04Z). Deliberately
# NOT included: "tracking-only from here" -- that exact phrase is routine boilerplate marie
# posts on EVERY Part C2b decomposition (marie.md:322, "...tracking-only from here, closes
# once every child is closed"), written before any child has closed. Treating it as its own
# marker would, combined with AC5's marker-wins-with-no-closedAt-data fallback (the common
# case for a real GitHub sub-issues link, which carries no closedAt -- confirmed live on both
# #785 and #553), permanently block almost every decomposed epic, not just a genuine reopen.
STAYS_OPEN_RE = re.compile(r"stays open|not yet \(vp review\):", re.I)


def closing_numbers(pr_body: str) -> list[int]:
    seen: list[int] = []
    for m in CLOSE_RE.finditer(pr_body or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def acceptance_criteria(issue_body: str, comments: list[dict]) -> str:
    """The NEWEST '## Acceptance criteria' block across body + comments (marie re-scopes by
    posting a fresh PRD comment, never by editing the body -- same rule minion.md builds by)."""
    texts = [issue_body or ""] + [c.get("body") or "" for c in sorted(comments, key=lambda c: c.get("createdAt") or "")]
    found = ""
    for text in texts:
        m = AC_HEADING_RE.search(text)
        if not m:
            continue
        rest = text[m.end():]
        nxt = re.search(r"^#{1,4}\s", rest, re.M)
        block = rest[: nxt.start()] if nxt else rest
        if block.strip():
            found = block.strip()
    return found


def decomposed_children(iss: dict) -> list[int]:
    """Child issue numbers from marie's `decomposed into #a, #b, ...` comment (Part C2b of
    members/marie/marie.md). Newest matching comment wins, same rule acceptance_criteria()
    follows for PRD comments. Returns [] when no such comment exists."""
    children: list[int] = []
    for c in sorted(iss.get("comments") or [], key=lambda c: c.get("createdAt") or ""):
        m = DECOMPOSED_RE.search(c.get("body") or "")
        if m:
            children = [int(x) for x in re.findall(r"\d+", m.group(1))]
    return children


def epic_open_children(iss: dict, issues: dict[int, dict]) -> tuple[bool, list[int]]:
    """(children_found, open_child_numbers) for an epic issue. Prefers GitHub's real
    `subIssues` structure (marie links these via Part C0, fk#652); falls back to her
    `decomposed into #a, #b` comment for an epic not yet linked. Returns (False, []) when
    neither source names any child -- an unlinked epic must never read as closable just
    because this gate cannot see what is under it."""
    sub = iss.get("subIssues")
    nodes = sub.get("nodes") if isinstance(sub, dict) else None
    if nodes:
        return True, [c["number"] for c in nodes if c.get("state") == "OPEN"]
    children = decomposed_children(iss)
    if not children:
        return False, []
    return True, [n for n in children if (issues.get(n) or {}).get("state") == "OPEN"]


def newest_child_closed_at(iss: dict, issues: dict[int, dict]) -> str | None:
    """ISO timestamp of this epic's most recently closed child, or None when no child carries
    one -- real GitHub `subIssues` nodes carry no `closedAt` (confirmed live on #785/#553;
    fetching it per child would be a second round-trip Non-goal 5 rules out), and a
    `decomposed into` child that failed to fetch carries none either. Callers must treat None
    as "cannot compare", not as "no child ever closed"."""
    sub = iss.get("subIssues")
    nodes = sub.get("nodes") if isinstance(sub, dict) else None
    if nodes:
        dates = [n.get("closedAt") for n in nodes if n.get("state") == "CLOSED"]
    else:
        dates = [(issues.get(n) or {}).get("closedAt") for n in decomposed_children(iss)]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def stays_open_marker(iss: dict) -> tuple[str, str] | None:
    """The newest comment on the epic's OWN thread matching a documented stays-open marker
    (STAYS_OPEN_RE) -- (marker text, comment createdAt), or None. Newest wins, same
    newest-comment convention acceptance_criteria() and decomposed_children() already use."""
    best: tuple[str, str] | None = None
    for c in sorted(iss.get("comments") or [], key=lambda c: c.get("createdAt") or ""):
        m = STAYS_OPEN_RE.search(c.get("body") or "")
        if m:
            best = (m.group(0), c.get("createdAt") or "")
    return best


def epic_closable(iss: dict, issues: dict[int, dict]) -> dict:
    """Whether an epic issue can be closed right now -- jefe's own pre-close check
    (members/jefe/jefe.md), independent of any PR. {"closable": bool, "reason": str}.
    See the module docstring for the three distinct meanings `closable: true` can carry."""
    found, open_children = epic_open_children(iss, issues)
    if open_children:
        return {
            "closable": False,
            "reason": "unaccepted children: " + ", ".join(f"#{c}" for c in open_children),
        }
    marker = stays_open_marker(iss)
    if marker:
        marker_text, marker_at = marker
        newest_closed = newest_child_closed_at(iss, issues) if found else None
        if newest_closed is None or marker_at > newest_closed:
            return {
                "closable": False,
                "reason": f'stays-open marker "{marker_text}" posted {marker_at} '
                + (
                    f"is newer than the last child close ({newest_closed})"
                    if newest_closed
                    else "and no child-close timestamp is available to outrank it"
                ),
            }
    if not found:
        return {
            "closable": True,
            "reason": "no children found (no real GitHub sub-issues, no `decomposed into` "
            "comment) -- an unlinked epic is never blocked on epic grounds",
        }
    return {"closable": True, "reason": "every child is closed"}


def evaluate(pr_body: str, pr_files: list[str], issues: dict[int, dict]) -> dict:
    """Pure: no network. issues = {number: {title, labels[], body, comments[{body,createdAt}],
    subIssues?, state?}}. `subIssues`/`state` are only needed for an issue that carries
    `fleet:epic` and/or is named as a child of one (see epic_open_children)."""
    reasons: list[str] = []
    intent_parts: list[str] = []
    partial = PARTIAL_RE.search(pr_body or "")
    docs_only = bool(pr_files) and all(DOC_PATH_RE.match(p) for p in pr_files)
    docs_or_test_only = bool(pr_files) and all(
        DOC_PATH_RE.match(p) or TEST_PATH_RE.search(p) for p in pr_files
    )
    for n in closing_numbers(pr_body):
        iss = issues.get(n)
        if not iss:
            intent_parts.append(f"#{n}: (issue could not be read)")
            continue
        labels = [l if isinstance(l, str) else l.get("name", "") for l in iss.get("labels") or []]
        lanes = [l for l in labels if l.startswith("lane:")]
        epic = EPIC in labels
        ac = acceptance_criteria(iss.get("body") or "", iss.get("comments") or [])
        if partial:
            reasons.append(
                f"PR says it is partial (\"{partial.group(0)}\") but closes #{n}. A PR that does not finish "
                f"the issue must say `Part of #{n}` and list what remains, not `Closes`/`Fixes`."
            )
        if docs_only and lanes and "lane:docs" not in lanes:
            reasons.append(
                f"PR changes only docs ({len(pr_files)} file(s)) but closes #{n}, a {', '.join(lanes)} item. "
                f"Prose cannot finish a product item: write `Part of #{n}`."
            )
        if epic and docs_or_test_only:
            reasons.append(
                f"PR changes only docs/tests but closes #{n}, an epic (`fleet:epic`). jefe closes "
                f"epics, not PRs, and only once every child is accepted -- write `Part of #{n}` instead."
            )
        elif epic:
            found, open_children = epic_open_children(iss, issues)
            if found and open_children:
                reasons.append(
                    f"#{n} is an epic (`fleet:epic`) with unaccepted children: "
                    + ", ".join(f"#{c}" for c in open_children)
                    + f". jefe closes epics, not PRs -- write `Part of #{n}` and let jefe close it "
                    "once every child is accepted."
                )
        spec = ac if ac else (iss.get("body") or "").strip()[:1500] or "(empty issue body)"
        intent_parts.append(
            f"#{n} {iss.get('title', '')}\n" + ("Acceptance criteria:\n" if ac else "No acceptance-criteria block; the issue body is the spec:\n") + spec
        )
    return {
        "verdict": "block" if reasons else "ok",
        "closes": closing_numbers(pr_body),
        "reasons": reasons,
        "intent": "\n\n".join(intent_parts),
    }


def gh_json(args: list[str]) -> dict | list | None:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None


def fetch_issue_and_epic_children(n: int, repo: list[str]) -> dict[int, dict]:
    """Fetch issue #n plus, if it is an epic with no real `subIssues` link, the state of
    each child named in marie's `decomposed into` comment. Returns {number: issue-dict},
    keyed the way evaluate()/epic_closable() expect."""
    issues: dict[int, dict] = {}
    iss = gh_json(
        ["issue", "view", str(n), *repo, "--json", "title,labels,body,comments,subIssues,state"]
    )
    if not isinstance(iss, dict):
        return issues
    issues[n] = iss
    labels = [l if isinstance(l, str) else l.get("name", "") for l in iss.get("labels") or []]
    sub = iss.get("subIssues")
    nodes = sub.get("nodes") if isinstance(sub, dict) else None
    if EPIC in labels and not nodes:
        # no real sub-issue links -- fetch state+closedAt for marie's "decomposed into"
        # children (closedAt feeds epic_closable()'s stays-open-marker timestamp compare)
        for child_n in decomposed_children(iss):
            if child_n not in issues:
                child_iss = gh_json(
                    ["issue", "view", str(child_n), *repo, "--json", "state,closedAt"]
                )
                if isinstance(child_iss, dict):
                    issues[child_n] = child_iss
    return issues


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="block a PR that closes an issue it does not finish")
    ap.add_argument("pr", type=int, nargs="?", help="PR number to gate (mutually exclusive with --epic)")
    ap.add_argument(
        "--epic",
        type=int,
        help="check whether THIS issue (an epic) can be closed right now, no PR involved "
        "-- the check members/jefe/jefe.md runs before it closes an epic (fk#652). "
        "closable:true means one of three things: goal met, no children discoverable, or "
        "a stays-open marker was outranked by a later child close -- read `reason` (fk#879)",
    )
    ap.add_argument("--repo", help="owner/name (default: the current repo)")
    a = ap.parse_args(argv)
    repo = ["--repo", a.repo] if a.repo else []

    if a.epic is not None:
        issues = fetch_issue_and_epic_children(a.epic, repo)
        iss = issues.get(a.epic, {})
        result = epic_closable(iss, issues)
        result["issue"] = a.epic
        json.dump(result, sys.stdout)
        print()
        return 0 if result["closable"] else 1

    if a.pr is None:
        ap.error("pr is required unless --epic is given")
    pr = gh_json(["pr", "view", str(a.pr), *repo, "--json", "body,files"]) or {}
    body = pr.get("body") or ""
    files = [f.get("path", "") for f in pr.get("files") or []]
    issues = {}
    for n in closing_numbers(body):
        issues.update(fetch_issue_and_epic_children(n, repo))
    result = evaluate(body, files, issues)
    result["pr"] = a.pr
    json.dump(result, sys.stdout)
    print()
    return 1 if result["verdict"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
