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
     (lane:ui, lane:backend, ...) -- prose cannot finish a product item.

Everything else is left to the reviewer, who now sees the issue's acceptance criteria and is
told to block unless the diff meets every one. The fix for the author is always the same one
word: write `Part of #N` and list what remains.

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
AC_HEADING_RE = re.compile(r"^#{1,4}\s*acceptance criteria\b.*$", re.I | re.M)


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


def evaluate(pr_body: str, pr_files: list[str], issues: dict[int, dict]) -> dict:
    """Pure: no network. issues = {number: {title, labels[], body, comments[{body,createdAt}]}}."""
    reasons: list[str] = []
    intent_parts: list[str] = []
    partial = PARTIAL_RE.search(pr_body or "")
    docs_only = bool(pr_files) and all(DOC_PATH_RE.match(p) for p in pr_files)
    for n in closing_numbers(pr_body):
        iss = issues.get(n)
        if not iss:
            intent_parts.append(f"#{n}: (issue could not be read)")
            continue
        labels = [l if isinstance(l, str) else l.get("name", "") for l in iss.get("labels") or []]
        lanes = [l for l in labels if l.startswith("lane:")]
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="block a PR that closes an issue it does not finish")
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", help="owner/name (default: the current repo)")
    a = ap.parse_args(argv)
    repo = ["--repo", a.repo] if a.repo else []
    pr = gh_json(["pr", "view", str(a.pr), *repo, "--json", "body,files"]) or {}
    body = pr.get("body") or ""
    files = [f.get("path", "") for f in pr.get("files") or []]
    issues: dict[int, dict] = {}
    for n in closing_numbers(body):
        iss = gh_json(["issue", "view", str(n), *repo, "--json", "title,labels,body,comments"])
        if isinstance(iss, dict):
            issues[n] = iss
    result = evaluate(body, files, issues)
    result["pr"] = a.pr
    json.dump(result, sys.stdout)
    print()
    return 1 if result["verdict"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
