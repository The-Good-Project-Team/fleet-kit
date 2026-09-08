#!/usr/bin/env python3
"""vp_due -- which quality:world-class items need a VP review right now?

THE GAP THIS CLOSES. The VP review (members/vp/vp.md) was meant to run every time a research
pass or a build slice for a world-class item merged. gru.md said so, but gru is a prompt: on
2026-09-08 the redo of philanthropy#4863 merged at 14:12Z and no gru pass spawned vp in the
next 2.5 hours (gru ran the item through fanout as a build candidate instead, then the
dead-end filter marked it BLOCKED). Reif: "I'm not paying money just so we can deny building
stuff. Preference is that we get the spec up to par." The loop that gets a spec up to par has
to be deterministic, so this is a script on cron (vp_due.sh, every 15 minutes), not a step
in a charter.

THE RULE. An open `quality:world-class` item is due for a VP review when a merged PR that
references it is newer than the newest VP verdict on it (or there is no verdict yet), unless a
comment starting `Reif:` is newer than that merge (his veto or instruction wins), and no vp
pass is already running for it.

Pure core (`is_due`, `due_items`), thin `gh` seam (`collect`), CLI (`main`) -- same split as
vision_link_gate.py and quality_gate.py.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

VERDICT_RE = re.compile(r"^\W*(design approved|accepted|not yet)\s*\(vp review\)\s*:", re.IGNORECASE | re.MULTILINE)
REIF_RE = re.compile(r"^\W*reif\s*:", re.IGNORECASE | re.MULTILINE)


def _newest(stamps):
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def is_due(item: dict, running: set[int] | None = None) -> tuple[bool, str]:
    """item = {number, comments:[{body, createdAt}], merged_prs:[{number, mergedAt}]}.
    ISO-8601 Zulu timestamps compare correctly as strings."""
    n = item["number"]
    if running and n in running:
        return False, "vp already running"
    merge = _newest(pr.get("mergedAt") for pr in item.get("merged_prs") or [])
    if not merge:
        return False, "nothing merged yet"
    comments = item.get("comments") or []
    verdict = _newest(c.get("createdAt") for c in comments if VERDICT_RE.search(c.get("body") or ""))
    reif = _newest(c.get("createdAt") for c in comments if REIF_RE.search(c.get("body") or ""))
    if reif and reif > merge:
        return False, "a Reif: comment is newer than the last merge"
    if verdict and verdict >= merge:
        return False, "verdict is newer than the last merge"
    return True, ("no verdict yet" if not verdict else "merge newer than last verdict")


def due_items(items: list[dict], running: set[int] | None = None) -> dict:
    due, skipped = [], []
    for it in items:
        ok, why = is_due(it, running)
        (due if ok else skipped).append({"number": it["number"], "why": why})
    return {"due": [d["number"] for d in due], "skipped": skipped}


def _gh(args: list[str], cwd: str) -> list:
    out = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout or "[]")


def collect(repo_dir: str) -> list[dict]:
    """Open world-class items with their comments and the merged PRs that mention them."""
    issues = _gh(["issue", "list", "--state", "open", "--label", "quality:world-class", "--limit", "100",
                  "--json", "number,comments"], repo_dir)
    items = []
    for iss in issues:
        n = iss["number"]
        prs = _gh(["pr", "list", "--state", "merged", "--search", f"#{n} in:body", "--limit", "50",
                   "--json", "number,mergedAt,body"], repo_dir)
        ref = re.compile(rf"(?<![\w/])#{n}(?!\d)")
        merged = [{"number": p["number"], "mergedAt": p.get("mergedAt")} for p in prs if ref.search(p.get("body") or "")]
        items.append({"number": n,
                      "comments": [{"body": c.get("body"), "createdAt": c.get("createdAt")} for c in iss.get("comments") or []],
                      "merged_prs": merged})
    return items


def running_vp_items() -> set[int]:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return {int(m) for m in re.findall(r"run_member\.sh vp --item (\d+)", out)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo-dir", required=True, help="the product checkout gh should read (FLEET_REPO)")
    p.add_argument("--items", help="JSON list of items (skips gh; for tests)")
    a = p.parse_args(argv)
    items = json.loads(a.items) if a.items else collect(a.repo_dir)
    print(json.dumps(due_items(items, running_vp_items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
