#!/usr/bin/env python3
"""rework_collect.py -- how much of what we merge is redoing what we already merged (fk#808).

THE GAP. dumbledore is the only member accountable for the health of the system that produces
the work, and self_improve_score.sh grades it on the predictions ledger -- the right design.
But no metric named the fleet's largest observable failure mode, so no prediction row could
ever resolve against it. Measured 2026-09-10 over 400 merged PRs on the product repo: 201 (50%)
carried fix/regression-shaped titles, and the add/delete ratio ran 13:1 for deploy/ops
(+14,301/-1,077). dumbledore was optimising a score that excluded all of it.

WHY A SEPARATE COLLECTOR. Every metric in fleet_metrics.py reads runs.jsonl (member telemetry).
Rework lives in GitHub, not in runs.jsonl, and fleet_metrics.compute() must stay a pure function
over rows it is handed -- so this collector writes a small cache and the metric reads it. Same
split as cost_bridge.py: network here, arithmetic there.

    python3 scripts/rework_collect.py --repo OWNER/REPO --limit 400          # write the cache
    python3 scripts/rework_collect.py --repo OWNER/REPO --limit 400 --print  # and show it

TWO SIGNALS, deliberately kept apart because they are not equally trustworthy:

  title_rework_pct  -- share of merged PRs whose TITLE reads like a redo. Cheap, directional,
                       and wrong by roughly +/-10pp: "fix the outreach email's price mismatch"
                       is rework, "fix(analytics): profile/viewed ignores crawlers" may be a
                       first implementation. Never gate on this alone.
  churn_ratio       -- additions/deletions across the window. A healthy codebase that is
                       genuinely growing runs high; one that rewrites itself runs high too, so
                       read it next to the title signal, not instead of it.

The honest version of the title signal is a file-touch test (did this PR modify a file first
added by a PR merged in the last N days). That needs per-PR file lists -- one API call each,
400 calls -- so it is left as the documented upgrade path rather than shipped slow.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Titles that read like redoing something already shipped. Tuned against the 400-PR sample:
# these matched 201. Kept as one regex so the definition is auditable in one line.
REWORK_TITLE_RE = re.compile(
    r"\b(stops?|no longer|fix(es|ed)?|again|really|actually|drift|mismatch|gaps?|"
    r"stop claiming|revert|regress|broken|breaks?)\b", re.I)

CACHE = Path(__file__).resolve().parent.parent / "state" / "rework.json"


def classify_title(title: str) -> bool:
    """True when a merged PR's title reads like rework rather than new work."""
    return bool(REWORK_TITLE_RE.search(title or ""))


def summarize(prs: list[dict]) -> dict:
    """Pure: PR rows in, metric row out. No network, so this is what the tests drive."""
    if not prs:
        return {"merged": 0, "title_rework": 0, "title_rework_pct": None,
                "additions": 0, "deletions": 0, "churn_ratio": None}
    rework = [p for p in prs if classify_title(p.get("title", ""))]
    adds = sum(int(p.get("additions") or 0) for p in prs)
    dels = sum(int(p.get("deletions") or 0) for p in prs)
    return {
        "merged": len(prs),
        "title_rework": len(rework),
        "title_rework_pct": round(100.0 * len(rework) / len(prs), 1),
        "additions": adds,
        "deletions": dels,
        # None, not 0 or infinity: a window that deleted nothing has no meaningful ratio, and
        # a fabricated one would resolve a prediction row on a number nobody measured.
        "churn_ratio": round(adds / dels, 2) if dels else None,
        "examples": [p.get("title", "")[:70] for p in rework[:5]],
    }


def fetch(repo: str, limit: int) -> list[dict]:
    out = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "merged", "--limit", str(limit),
         "--json", "number,title,additions,deletions,mergedAt"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh pr list: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout or "[]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument("--print", action="store_true", dest="show")
    a = ap.parse_args(argv)

    try:
        prs = fetch(a.repo, a.limit)
    except RuntimeError as e:
        print(f"rework_collect: {e}", file=sys.stderr)
        return 2

    row = summarize(prs)
    row.update({"repo": a.repo, "limit": a.limit, "ts": time.time()})
    a.cache.parent.mkdir(parents=True, exist_ok=True)
    a.cache.write_text(json.dumps(row, indent=2))

    if a.show:
        print(f"{row['title_rework']}/{row['merged']} merged PRs read as rework "
              f"({row['title_rework_pct']}%)")
        print(f"  +{row['additions']} / -{row['deletions']}  churn_ratio={row['churn_ratio']}")
        for t in row.get("examples") or []:
            print(f"    {t}")
    else:
        print(f"rework_collect: wrote {a.cache} ({row['title_rework_pct']}% of {row['merged']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
