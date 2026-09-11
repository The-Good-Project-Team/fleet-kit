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

THE RULE. (Also: a `Not yet` verdict with no newer merge and fewer than three rounds spawns
the redo minion -- see redo_due.) An open `quality:world-class` item is due for a VP review when a merged PR that
references it is newer than the newest VP verdict on it (or there is no verdict yet), unless a
comment starting `Reif:` is newer than that merge (his veto or instruction wins), and no vp
pass is already running for it, and it has had fewer than MAX_ROUNDS `Not yet` rounds -- the
same cap `redo_due` puts on the builder now binds the reviewer too (see MAX_ROUNDS below).

Pure core (`is_due`, `due_items`), thin `gh` seam (`collect`), CLI (`main`) -- same split as
vision_link_gate.py and quality_gate.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

VERDICT_RE = re.compile(r"^\W*(design approved|accepted|not yet)\s*\(vp review\)\s*:", re.IGNORECASE | re.MULTILINE)
REIF_RE = re.compile(r"^\W*reif\s*:", re.IGNORECASE | re.MULTILINE)
NOT_YET_RE = re.compile(r"^\W*not yet\s*\(vp review\)\s*:", re.IGNORECASE | re.MULTILINE)
ISSUE_REF_RE = re.compile(r"#(\d+)")
EPIC_LABEL = "fleet:epic"

# THE SAME CAP BINDS BOTH SIDES OF THE LOOP (fleet-kit#798). MAX_ROUNDS used to bound only
# `redo_due` -- the BUILDER. `is_due` -- the REVIEWER -- had no cap, so an item that had been
# denied three times could not be built and could still be re-reviewed, forever, every time
# any PR mentioning it merged. Live on project-sketchyswap#5 (2026-09-09): six `Not yet (VP
# review):` rounds in 13 hours, `redo_skipped: "6 Not-yet rounds: marie re-scopes, no more
# redos"`, zero builder passes, ~$14 of vp re-denials on the highest-value item on the board
# while the P0 that blocks the instance's only number (#62, nobody can sign in) got none. A
# spec that has failed the bar three times does not need a fourth opinion; it needs a person
# to re-scope or drop it. The release is deliberately a human one: a `Reif:` comment newer
# than the newest denial lifts the cap, the same override vp.md already gives him.
MAX_ROUNDS = 3


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
    not_yets = sorted(c.get("createdAt") or "" for c in comments if NOT_YET_RE.search(c.get("body") or ""))
    if len(not_yets) >= MAX_ROUNDS and not (reif and reif > not_yets[-1]):
        return False, f"{len(not_yets)} Not-yet rounds: a decision, not another review"
    if verdict and verdict >= merge:
        return False, "verdict is newer than the last merge"
    return True, ("no verdict yet" if not verdict else "merge newer than last verdict")



def redo_due(item: dict, running_minions: set[int] | None = None) -> tuple[bool, str]:
    """A `Not yet (VP review)` that is newer than the last merge is a work order (Reif,
    2026-09-08: "I'm not paying money just so we can deny building stuff"). vp itself cannot
    start the builder -- a child of its pass dies when the pass ends (16:53Z: minion started,
    minion killed, same minute). So cron does it: fewer than MAX_ROUNDS Not-yets, no minion
    running for the item, no `Reif:` comment newer than the verdict -> spawn the redo."""
    n = item["number"]
    if running_minions and n in running_minions:
        return False, "minion already running"
    comments = item.get("comments") or []
    not_yets = sorted(c.get("createdAt") or "" for c in comments if NOT_YET_RE.search(c.get("body") or ""))
    verdict = _newest(c.get("createdAt") for c in comments if VERDICT_RE.search(c.get("body") or ""))
    if not verdict or verdict not in not_yets:
        return False, "newest verdict is not a Not yet"
    merge = _newest(pr.get("mergedAt") for pr in item.get("merged_prs") or [])
    if merge and merge > verdict:
        return False, "a merge is newer than the Not yet (vp review is due instead)"
    reif = _newest(c.get("createdAt") for c in comments if REIF_RE.search(c.get("body") or ""))
    if reif and reif > verdict:
        return False, "a Reif: comment is newer than the verdict"
    if len(not_yets) >= MAX_ROUNDS:
        return False, f"{len(not_yets)} Not-yet rounds: marie re-scopes, no more redos"
    return True, f"Not yet round {len(not_yets)} needs its redo"


def running_minion_items() -> set[int]:
    return running_items(_runs_rows(), "minion")


def due_items(items: list[dict], running: set[int] | None = None) -> dict:
    due, skipped = [], []
    for it in items:
        ok, why = is_due(it, running)
        (due if ok else skipped).append({"number": it["number"], "why": why})
    return {"due": [d["number"] for d in due], "skipped": skipped}


def _label_names(labels) -> list[str]:
    return [lab.get("name", "") if isinstance(lab, dict) else str(lab) for lab in labels or []]


def is_epic(item: dict) -> bool:
    return EPIC_LABEL in _label_names(item.get("labels"))


def named_children(item: dict) -> list[int]:
    """Issue numbers the newest `Not yet (VP review):` comment names, in the order they first
    appear, excluding the item's own number. Text parse only -- open/closed state (and PR vs.
    issue) is resolved separately via `gh` in `redo_targets`, since the same prose also names
    the merged PR that triggered the round and children that already closed."""
    not_yets = [c for c in item.get("comments") or [] if NOT_YET_RE.search(c.get("body") or "")]
    if not not_yets:
        return []
    newest = max(not_yets, key=lambda c: c.get("createdAt") or "")
    seen: set[int] = set()
    out: list[int] = []
    for m in ISSUE_REF_RE.finditer(newest.get("body") or ""):
        n = int(m.group(1))
        if n != item["number"] and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def redo_targets(item: dict, is_open) -> tuple[list[int], str]:
    """fk#634 round-2 fix 1: PR#842 closed the gru dispatch door onto a tracking-only epic, but
    left this one open -- `vp_due.sh` spawns a redo minion straight at the epic itself. A
    non-epic redo is unchanged: it targets itself. An epic redo targets each open child issue
    the newest `Not yet (VP review):` comment names; with none open, it falls back to the epic
    itself so epic-level work (fixes with no child owner, like this one) is never starved."""
    if not is_epic(item):
        return [item["number"]], "single item"
    open_kids = [n for n in named_children(item) if is_open(n)]
    if open_kids:
        return open_kids, f"{len(open_kids)} open child issue(s) named in the verdict"
    return [item["number"]], "epic-level redo: no child named"


def redo_items(items: list[dict], running_minions: set[int] | None = None, is_open=None) -> dict:
    is_open = is_open or (lambda n: True)
    due, skipped = [], []
    for it in items:
        ok, why = redo_due(it, running_minions)
        if not ok:
            skipped.append({"number": it["number"], "why": why})
            continue
        targets, reason = redo_targets(it, is_open)
        due.append({"number": it["number"], "targets": targets, "why": reason})
    return {"redo": due, "redo_skipped": skipped}


def _gh(args: list[str], cwd: str) -> list:
    out = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout or "[]")


# fk#805: vp reviewed 3 of 191 issues because this label was hardcoded to world-class alone.
# quality:solid is where the user-facing surfaces live (76 items vs 3); ship-it stays ungated so
# throughput is unaffected. Override with FLEET_VP_LABELS (comma-separated) to widen or narrow
# without a code change.
VP_LABELS = tuple(
    lbl.strip()
    for lbl in os.environ.get("FLEET_VP_LABELS", "quality:world-class,quality:solid").split(",")
    if lbl.strip()
)


def _claims_item(body: str, n: int) -> bool:
    """True only if `body` has a line CLAIMING item #n as a build slice -- `Fixes #n`,
    `Closes #n`, or `Part of #n` (gru's own charter draws this same line, minion.md step 1) --
    never a bare `#n` mention in ordinary prose.

    gh#636 (VP round-2 fix 7): PR #842 merged with `#636` only inside a sentence describing
    which epics a NEW guard now drops ("Tracking epics stop looking buildable to gru... #636
    ..."), and the old bare-`#n` regex here read that as a merged build slice for #636 -- the
    same PR that also, unrelated to #636, taught quality_gate.py to skip `fleet:epic` items.
    A claim is matched case-insensitively, anywhere a line starts (allowing markdown/list
    prefixes), same tolerance VERDICT_RE above already uses for verdict lines."""
    claim_re = re.compile(
        rf"^\W*(fixes|closes|resolves|part of)\s*:?\s*#{n}(?!\d)", re.IGNORECASE | re.MULTILINE
    )
    return bool(claim_re.search(body or ""))


def collect(repo_dir: str) -> list[dict]:
    """Open items in any VP_LABELS tier, with their comments and the merged PRs that mention them."""
    issues = []
    seen: set[int] = set()
    for label in VP_LABELS:
        for iss in _gh(["issue", "list", "--state", "open", "--label", label, "--limit", "100",
                        "--json", "number,comments,labels"], repo_dir):
            # an item carrying two quality labels must not be collected (or reviewed) twice
            if iss["number"] not in seen:
                seen.add(iss["number"])
                issues.append(iss)
    items = []
    for iss in issues:
        n = iss["number"]
        prs = _gh(["pr", "list", "--state", "merged", "--search", f"#{n} in:body", "--limit", "50",
                   "--json", "number,mergedAt,body"], repo_dir)
        merged = [{"number": p["number"], "mergedAt": p.get("mergedAt")} for p in prs
                  if _claims_item(p.get("body") or "", n)]
        items.append({"number": n,
                      "labels": _label_names(iss.get("labels")),
                      "comments": [{"body": c.get("body"), "createdAt": c.get("createdAt")} for c in iss.get("comments") or []],
                      "merged_prs": merged})
    return items


def _issue_is_open(repo_dir: str, n: int) -> bool:
    """fk#634 round-2 fix 1: is issue #n (a candidate child named in a redo verdict) open? A PR
    number named in the same prose (e.g. the merged PR that triggered the round) fails here too
    -- `gh issue view` on a PR number errors, which this treats the same as closed/nonexistent."""
    out = subprocess.run(["gh", "issue", "view", str(n), "--json", "state"], cwd=repo_dir,
                          capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        return False
    try:
        return json.loads(out.stdout).get("state") == "OPEN"
    except ValueError:
        return False


RUN_TIMEOUT_S = 2400  # minion and vp both carry timeout_s 2400 in their fleet.json


def running_items(rows: list[dict], member: str, now: float | None = None,
                  timeout_s: int = RUN_TIMEOUT_S) -> set[int]:
    """Items with a pass of `member` in flight, read from runs.jsonl rows (shared by every
    container of this instance). A pass is in flight when its newest row is `started` and
    younger than the member's timeout. `ps` cannot see a pass in the retired container after a
    rolling cutover: 2026-09-08 19:54Z vp_due spawned a second minion on #4863 while the first,
    started 6 minutes earlier in the container that had just been retired, was still working.
    `rows` in file order (oldest first); the newest row per run_id wins."""
    now = time.time() if now is None else now
    newest: dict[str, dict] = {}
    for r in rows:
        if r.get("member") == member and r.get("item_id") not in (None, "", "None"):
            newest[r.get("run_id")] = r
    out = set()
    for r in newest.values():
        if r.get("status") == "started" and now - float(r.get("ts") or 0) < timeout_s:
            try:
                out.add(int(r["item_id"]))
            except (TypeError, ValueError):
                pass
    return out


def _runs_rows() -> list[dict]:
    path = os.path.join(os.environ.get("FLEET_LOG_DIR", "/var/log/fleet-kit"), "runs.jsonl")
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def running_vp_items() -> set[int]:
    return running_items(_runs_rows(), "vp")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo-dir", required=True, help="the product checkout gh should read (FLEET_REPO)")
    p.add_argument("--items", help="JSON list of items (skips gh; for tests)")
    a = p.parse_args(argv)
    items = json.loads(a.items) if a.items else collect(a.repo_dir)
    out = due_items(items, running_vp_items())
    out.update(redo_items(items, running_minion_items(), is_open=lambda n: _issue_is_open(a.repo_dir, n)))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
