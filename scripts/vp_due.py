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
import calendar
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import closes_gate  # noqa: E402 -- fk#854: reuse the one parser marie's `decomposed into`
# comment already has (fk#882's edge cases: ranges, slash lists, per-child parentheticals),
# rather than growing a second one here that drifts from it.

VERDICT_RE = re.compile(r"^\W*(design approved|accepted|not yet)\s*\(vp review\)\s*:", re.IGNORECASE | re.MULTILINE)
REIF_RE = re.compile(r"^\W*reif\s*:", re.IGNORECASE | re.MULTILINE)
NOT_YET_RE = re.compile(r"^\W*not yet\s*\(vp review\)\s*:", re.IGNORECASE | re.MULTILINE)
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


def real_children(item: dict) -> tuple[list[int], str]:
    """An epic's real children, and which source produced them -- GitHub's own `subIssues`
    link when marie has made one (fk#652), else her `decomposed into #a, #b, ...` comment
    (`closes_gate.decomposed_children`, the same source `closes_gate.py` already trusts to
    decide whether an epic can close). Never a number merely NAMED in a verdict's prose: fk#634
    round-3 measured `named_children()`'s old bare `#(\\d+)` scrape returning 10 "targets" on
    #634, 5 of them other teams' tracking epics, named only because the verdict was *counting*
    open epics ("all 6 open fleet:epic issues now drop..."). Returns ([], "no source") when
    neither source names anything -- `redo_targets` falls back to the epic itself, same as an
    all-closed child list."""
    sub = item.get("subIssues")
    nodes = sub.get("nodes") if isinstance(sub, dict) else None
    if nodes:
        return [n["number"] for n in nodes], "subIssues"
    children = closes_gate.decomposed_children(item)
    return children, ("a `decomposed into` comment" if children else "no source")


def redo_targets(item: dict, is_open) -> tuple[list[int], str]:
    """fk#634 round-2 fix 1: PR#842 closed the gru dispatch door onto a tracking-only epic, but
    left this one open -- `vp_due.sh` spawns a redo minion straight at the epic itself. A
    non-epic redo is unchanged: it targets itself. An epic redo targets each open REAL child
    (`real_children`, never a number merely named in the verdict's prose); `is_open` also
    excludes a candidate that itself carries `fleet:epic` (fk#854 AC2 -- a real child can be
    another team's parent, and a builder can no more build that than the epic that named it).
    With no open real child, it falls back to the epic itself so epic-level work (fixes with no
    child owner, like this one) is never starved."""
    if not is_epic(item):
        return [item["number"]], "single item"
    children, source = real_children(item)
    open_kids = [n for n in children if is_open(n)]
    if open_kids:
        return open_kids, f"{len(open_kids)} open child issue(s) via {source}"
    return [item["number"]], "epic-level redo: no child named"


# ONE DISPATCH PER VERDICT, NOT ONE PER HOUR (fleet-kit#883). `redo_due` caps the number of
# `Not yet` ROUNDS at MAX_ROUNDS, but nothing capped the number of DISPATCHES per round: a
# single un-superseded `Not yet` re-spawns its redo minion on every cron tick, forever, because
# the condition that armed it ("newest verdict is a Not yet, no merge newer") only clears when a
# PR merges. Measured on this instance 2026-09-12: the identical 16-target redo list dispatched
# at 13:04, 14:04 and 15:03 (`vp_due.log`), 35 minion passes onto philanthropy#5237 in 23h, and
# 171 of 196 minion passes that day ended "already done / blocked" with no PR. `running_minions`
# did not catch it -- it is checked against the SOURCE issue, while an epic's redo spawns onto
# its CHILDREN, so a running minion on #5237 never suppressed the next tick's dispatch at #5237.
#
# The rule: a target is dispatched once for a given verdict. It re-arms when a newer `Not yet`
# lands (a real new work order), or after REDO_RETRY_AFTER_S, so a redo whose minion crashed or
# was budget-declined still gets another try -- bounded at 2/day instead of 24/day rather than
# traded away entirely.
REDO_RETRY_AFTER_S = int(os.environ.get("FLEET_REDO_RETRY_AFTER_S", 12 * 3600))


def _iso_to_epoch(stamp: str | None) -> float | None:
    """`2026-09-12T13:28:22Z` -> epoch seconds. None for anything unparseable -- a missing
    timestamp must never make a guard fire, only make it abstain."""
    if not stamp:
        return None
    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def newest_not_yet_epoch(item: dict) -> float | None:
    """When this item's newest `Not yet (VP review):` comment landed -- the moment its redo was
    armed. Everything dispatched after it belongs to that verdict."""
    stamps = [c.get("createdAt") for c in item.get("comments") or []
              if NOT_YET_RE.search(c.get("body") or "")]
    return _iso_to_epoch(_newest(stamps))


def last_dispatch_at(rows: list[dict], member: str) -> dict[int, float]:
    """Newest run timestamp per item for `member`, epoch seconds -- read from the same
    runs.jsonl rows `running_items` uses, so it sees passes in a retired container too."""
    out: dict[int, float] = {}
    for r in rows:
        if r.get("member") != member or r.get("item_id") in (None, "", "None"):
            continue
        try:
            n = int(r["item_id"])
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if ts > out.get(n, 0):
            out[n] = ts
    return out


def last_minion_dispatch() -> dict[int, float]:
    return last_dispatch_at(_runs_rows(), "minion")


def redo_items(items: list[dict], running_minions: set[int] | None = None, is_open=None,
               dispatched: dict[int, float] | None = None, now: float | None = None) -> dict:
    is_open = is_open or (lambda n: True)
    dispatched = dispatched or {}
    now = time.time() if now is None else now
    running_minions = running_minions or set()
    due, skipped = [], []
    claimed: set[int] = set()   # targets this pass already spawned, under any source
    for it in items:
        ok, why = redo_due(it, running_minions)
        if not ok:
            skipped.append({"number": it["number"], "why": why})
            continue
        targets, reason = redo_targets(it, is_open)
        armed_at = newest_not_yet_epoch(it)
        kept = []
        for t in targets:
            drop = _redo_target_skip(t, armed_at, claimed, running_minions, dispatched, now)
            if drop:
                skipped.append({"number": t, "source": it["number"], "why": drop})
                continue
            claimed.add(t)
            kept.append(t)
        if kept:
            due.append({"number": it["number"], "targets": kept, "why": reason})
    return {"redo": due, "redo_skipped": skipped}


def _redo_target_skip(target: int, armed_at: float | None, claimed: set[int],
                      running_minions: set[int], dispatched: dict[int, float],
                      now: float) -> str | None:
    """Why this TARGET should not be spawned, or None to spawn it. Every guard here is
    target-scoped on purpose -- the source-scoped `running_minions` check in `redo_due` misses
    every epic child, which is most of what this script actually dispatches."""
    if target in claimed:
        return "already dispatched this pass under another source"
    if target in running_minions:
        return "minion already running for this target"
    prev = dispatched.get(target)
    if prev is None:
        return None
    if armed_at is not None and prev >= armed_at and now - prev < REDO_RETRY_AFTER_S:
        return (f"redo already dispatched for this verdict "
                f"({int((now - prev) / 60)}m ago, retry after {REDO_RETRY_AFTER_S // 3600}h)")
    return None


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
    A claim is matched case-insensitively, anywhere a line starts (allowing markdown bullets,
    bold markers, and a numbered-list prefix like `1. Fixes #n` -- `\W*` alone stops at a
    leading digit, since digits are word characters, so a numbered PR checklist needs its own
    allowance), same tolerance VERDICT_RE above already uses for verdict lines."""
    claim_re = re.compile(
        rf"^\W*(?:\d+[.)]\s*)?(fixes|closes|resolves|part of)\s*:?\s*#{n}(?!\d)",
        re.IGNORECASE | re.MULTILINE,
    )
    return bool(claim_re.search(body or ""))


def collect(repo_dir: str) -> list[dict]:
    """Open items in any VP_LABELS tier, with their comments, their real GitHub `subIssues`
    link (fk#854 AC1 -- `gh issue list --json subIssues` returns each child's number+state
    directly, no per-issue round-trip needed), and the merged PRs that mention them."""
    issues = []
    seen: set[int] = set()
    for label in VP_LABELS:
        for iss in _gh(["issue", "list", "--state", "open", "--label", label, "--limit", "100",
                        "--json", "number,comments,labels,subIssues"], repo_dir):
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
                      "subIssues": iss.get("subIssues"),
                      "merged_prs": merged})
    return items


def _valid_redo_target(repo_dir: str, n: int) -> bool:
    """fk#634 round-2 fix 1 (widened by fk#854 AC2): is issue #n a real child a redo minion may
    safely be dispatched at -- open, AND not itself carrying `fleet:epic`. A real child of one
    epic can be another team's tracking parent (a nested epic), and a builder can no more build
    that than the epic that named it could. A PR number that ends up here too (e.g. the merged
    PR that triggered the round, on the pre-fk#854 prose path) fails here too -- `gh issue view`
    on a PR number errors, which this treats the same as closed/nonexistent."""
    out = subprocess.run(["gh", "issue", "view", str(n), "--json", "state,labels"], cwd=repo_dir,
                          capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        return False
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return False
    if data.get("state") != "OPEN":
        return False
    return EPIC_LABEL not in _label_names(data.get("labels"))


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
    out.update(redo_items(items, running_minion_items(),
                          is_open=lambda n: _valid_redo_target(a.repo_dir, n),
                          dispatched=last_minion_dispatch()))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
