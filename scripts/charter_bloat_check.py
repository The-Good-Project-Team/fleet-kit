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

NEVER PRINT `ok` OVER DATA WE DID NOT READ (fk#908). The first version wrote
`_gh_json(...) or []`, so ANY gh failure -- rate limit, auth, timeout, network -- produced an
empty PR list, and an empty PR list is arithmetically identical to "no PR has touched any
charter": every member printed `since_consolidation=0 ... ok` and main() exited 0. jefe.md
reads that exit code as "no charter needs consolidating today" and moves on, so a GitHub quota
outage silently disarms the one daily duty fk#753 built this script to force. Measured live
2026-09-11: a rate-limited run gave all 17 charters a clean bill of health. Now the failure is
loud (exit 2, no per-charter rows at all), and before giving up the script falls back to local
git history -- which needs no API, no quota, and is exactly the same population, since every
merge here squash-lands on main carrying its `(#NNN)`.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys

REPO_SLUG_DEFAULT = "The-Good-Project-Team/fleet-kit"
CONSOLIDATION_THRESHOLD = 5


class SourceUnavailable(RuntimeError):
    """We could not READ the merged-PR list. Distinct from "we read it and it was empty"."""


def _gh_json(args, timeout=60):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise SourceUnavailable(f"{type(e).__name__}: {e}"[:200]) from e
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()
        raise SourceUnavailable((tail[-1] if tail else f"gh exited {out.returncode}")[:200])
    try:
        return json.loads(out.stdout)
    except ValueError as e:
        raise SourceUnavailable(f"gh returned unparseable JSON: {e}"[:200]) from e


def member_charter_paths(root: str, members_glob: str) -> list[str]:
    return sorted(
        p[len(root) + 1 :] if p.startswith(root + "/") else p
        for p in glob.glob(f"{root}/{members_glob}")
    )


def fetch_merged_prs(repo_slug: str, limit: int):
    """Merged PRs from the API. Raises SourceUnavailable rather than returning [] on failure."""
    return _gh_json(
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


def parse_git_numstat(text: str) -> list[dict]:
    """Pure: `git log --numstat` output in, the same PR rows analyze() takes out.

    Kept separate from the subprocess call so the parsing is what the tests drive. Each record
    is NUL-delimited and starts `<iso-date>\t<subject>`, then one `adds\tdels\tpath` line per
    file. A squash-merged subject ends `(#NNN)`; a direct push has no number, and gets its short
    SHA instead -- it still edited the charter, so it still counts as an additive pass.
    """
    rows = []
    for rec in text.split("\x00"):
        lines = [ln for ln in rec.splitlines() if ln.strip()]
        if not lines:
            continue
        head = lines[0].split("\t")
        if len(head) < 3:
            continue
        merged_at, sha, subject = head[0], head[1], head[2]
        m = re.search(r"\(#(\d+)\)\s*$", subject)
        files = []
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            adds, dels, path = parts
            if adds == "-" or dels == "-":  # binary file
                continue
            files.append({"path": path, "additions": int(adds), "deletions": int(dels)})
        rows.append({
            "number": int(m.group(1)) if m else sha[:9],
            "mergedAt": merged_at,
            "title": subject,
            "files": files,
        })
    return rows


def fetch_merged_prs_from_git(root: str, limit: int, ref: str) -> list[dict]:
    """The same population, read from local history: no API, no quota, always available."""
    fmt = "%x00%cI%x09%H%x09%s"
    try:
        out = subprocess.run(
            ["git", "-C", root, "log", ref, "--no-merges", "-n", str(limit),
             "--numstat", f"--pretty=format:{fmt}"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SourceUnavailable(f"{type(e).__name__}: {e}"[:200]) from e
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()
        raise SourceUnavailable((tail[-1] if tail else f"git exited {out.returncode}")[:200])
    return parse_git_numstat(out.stdout)


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
        # Sort by mergedAt only: `number` mixes int (squash-merged, `#NNN`) and str (direct
        # push, short SHA) across rows, and Python can't compare those -- sorting on the full
        # tuple raised TypeError whenever two rows tied on mergedAt's second precision.
        touching.sort(key=lambda t: t[0], reverse=True)  # newest first

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
    ap.add_argument("--git-ref", default="origin/main",
                    help="ref the git fallback reads when gh is unavailable")
    ap.add_argument("--no-git-fallback", action="store_true",
                    help="fail instead of falling back to local git history")
    args = ap.parse_args()

    paths = member_charter_paths(args.root, args.members_glob)
    if not paths:
        print(f"no files matched {args.members_glob} under {args.root}", file=sys.stderr)
        sys.exit(2)

    try:
        prs = fetch_merged_prs(args.repo, args.limit)
        source = f"gh pr list {args.repo}"
    except SourceUnavailable as gh_err:
        if args.no_git_fallback:
            return _no_verdict(f"gh unavailable ({gh_err}), --no-git-fallback set")
        print(f"charter_bloat_check: gh unavailable ({gh_err}) -- "
              f"falling back to local git history ({args.git_ref})", file=sys.stderr)
        try:
            prs = fetch_merged_prs_from_git(args.root, args.limit, args.git_ref)
            source = f"git log {args.git_ref} (gh unavailable: {gh_err})"
        except SourceUnavailable as git_err:
            return _no_verdict(f"gh ({gh_err}) and git ({git_err}) both unreadable")

    if not prs:
        return _no_verdict(f"{source} returned 0 merged PRs")

    results = analyze(paths, prs)

    # Stdout names its own provenance: a degraded run must never be mistakable for a clean one.
    print(f"# source: {source}\t scanned={len(prs)} merged PRs")
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

    return 1 if any_flagged else 0


def _no_verdict(reason: str) -> int:
    """Exit 2 with NO per-charter rows. `ok` over data we never read is the bug (fk#908):
    jefe.md treats exit 0 as 'no charter needs consolidating today', so a silent empty list
    retires the duty for the day. Say nothing rather than say something false."""
    print(f"charter_bloat_check: NO VERDICT -- {reason}. Printing no per-charter rows: an "
          f"unread PR list is indistinguishable from 'no charter was touched', and `ok` here "
          f"would be a false all-clear. Re-run when the source is readable.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
