#!/usr/bin/env python3
"""charter_bloat_check -- is any member's charter accumulating patches with no consolidation pass?

THE GAP THIS CLOSES (fk#753). jefe.md's charter-bloat duty told jefe to reconstruct each
member's consolidation history "against your own memory of last pass's counts, or the member's
recent PR list" -- prose with no forcing mechanism. In practice it never fired from jefe itself:
of the last 25 merged PRs touching `members/*/*.md`, 13 were dumbledore-authored consolidation
passes, 1 was jefe-authored, 11 were direct human feature additions (fleet-kit#746's corrected
authorship check). The duty survived by dumbledore's initiative, not by design. This script does
the reconstruction jefe was being asked to remember, so running one command replaces remembering.

`gh pr list --search "<path> in:files"` is NOT a real changed-files filter -- GitHub's PR search
has no such qualifier, so it text-matches PR body/title/comments and returns false positives (a
persona_law.md-only PR showing up under nerd.md's results, confirmed live in fk#753). This
instead pulls each merged PR's own `--json files` list directly and checks path membership in
Python.

A PR counts as a CONSOLIDATION pass, not an additive one, if its diff is net-reductive
(deletions >= additions, and non-zero) on that member's own file. Title keywords ("trim",
"consolidat", "tidied up", ...) are not used as the signal -- they're guessable, gameable, and
inconsistent across authors; a real line-count reduction on the file itself is not.
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys

REPO_SLUG_DEFAULT = "The-Good-Project-Team/fleet-kit"
CONSOLIDATION_THRESHOLD = 5


def _gh_json(args, timeout=60):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        print(f"gh call failed: gh {' '.join(args)}\n{out.stderr}", file=sys.stderr)
        return None
    return json.loads(out.stdout)


def member_charter_paths(root: str, members_glob: str) -> list[str]:
    return sorted(
        p[len(root) + 1 :] if p.startswith(root + "/") else p
        for p in glob.glob(f"{root}/{members_glob}")
    )


def fetch_merged_prs(repo_slug: str, limit: int):
    return (
        _gh_json(
            [
                "pr",
                "list",
                "--repo",
                repo_slug,
                "--state",
                "merged",
                "--json",
                "number,mergedAt,title,files",
                "--limit",
                str(limit),
            ],
            timeout=120,
        )
        or []
    )


def analyze(paths: list[str], prs: list[dict]) -> dict:
    results = {}
    for rel in paths:
        touching = []
        for pr in prs:
            match = next((f for f in (pr.get("files") or []) if f.get("path") == rel), None)
            if not match:
                continue
            touching.append(
                (
                    pr["mergedAt"],
                    pr["number"],
                    match.get("additions", 0),
                    match.get("deletions", 0),
                )
            )
        touching.sort(reverse=True)  # newest first

        since = 0
        last_consolidation_pr = None
        last_consolidation_date = None
        for merged_at, number, add, delete in touching:
            if delete >= add and (add + delete) > 0:
                last_consolidation_pr = number
                last_consolidation_date = merged_at
                break
            since += 1

        results[rel] = {
            "count_since_consolidation": since,
            "last_consolidation_pr": last_consolidation_pr,
            "last_consolidation_date": last_consolidation_date,
            "needs_consolidation": since >= CONSOLIDATION_THRESHOLD,
        }
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=REPO_SLUG_DEFAULT)
    ap.add_argument("--root", default=".", help="repo root containing members/")
    ap.add_argument("--members-glob", default="members/*/*.md")
    ap.add_argument("--limit", type=int, default=300, help="merged PRs to scan")
    args = ap.parse_args()

    paths = member_charter_paths(args.root, args.members_glob)
    if not paths:
        print(f"no files matched {args.members_glob} under {args.root}", file=sys.stderr)
        sys.exit(2)

    prs = fetch_merged_prs(args.repo, args.limit)
    results = analyze(paths, prs)

    any_flagged = False
    for rel in sorted(results):
        r = results[rel]
        flag = "NEEDS CONSOLIDATION" if r["needs_consolidation"] else "ok"
        any_flagged = any_flagged or r["needs_consolidation"]
        print(
            f"{rel}\tsince_consolidation={r['count_since_consolidation']}\t"
            f"last_consolidation_pr={r['last_consolidation_pr']}\t"
            f"last_consolidation_date={r['last_consolidation_date']}\t{flag}"
        )

    sys.exit(1 if any_flagged else 0)


if __name__ == "__main__":
    main()
