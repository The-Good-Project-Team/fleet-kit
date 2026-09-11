#!/usr/bin/env python3
"""vision_link_gate -- is this candidate eligible for gru to pick, gh#525?

THE GAP THIS CLOSES. gh#523 named the failure directly: gru picks purely from marie's
`fleet:priority-<tier>` labels by `createdAt` (gru.md step 2b) -- whether a candidate's own
PRD says it moves the number introduced by #513 ("Vision-link:") is not read at all. Result:
12 same-morning minion PRs, all `fix(...)` inward-spend, none evaluated against whether they
move the number.

THE RULE (gh#525's own Fix section, verbatim): a candidate is eligible only if its body/PRD
carries a `Vision-link:` line naming something real (the number, the guardrail, the channel --
free text is fine, per gh#525's own open question, until #513's `number.json` ships and this
can validate against it instead) -- OR it is explicitly `Vision-link: none (maintenance)` AND
no OTHER open candidate anywhere in the set carries a real Vision-link. A candidate with no
`Vision-link:` line at all -- neither a real link nor an explicit `none (maintenance)` -- is
never eligible on its own. marie's PRD template (marie.md Part C4) stamps this line on every
new/re-scored PRD as of PR#587, and gh#588 backfilled the pre-existing `fleet:prd` population --
but a PRD only exists at all for `fleet:priority-high` items, capped at 5/pass. Most of the
backlog (medium/low tier, or high-tier still waiting under the cap) never gets a `fleet:prd`
comment, so it never got this line either, until gh#4597: marie.md Part C4 now also runs an
uncapped, lightweight sweep posting a `Vision-link:`-only comment (no PRD) on every
`fleet:backlog` candidate that has neither `fleet:prd` nor an existing line -- this script does
not need to know or care which kind of comment supplied the line (see below).

WHERE THE LINE LIVES. A `Vision-link:` line can live directly in an issue BODY (Reif filing an
item names its link himself), in a `fleet:prd` PRD comment (marie scoring it), or in a
lightweight non-PRD comment (marie's gh#4597 backfill) -- this script reads every comment the
same way regardless of what kind it is or what label the issue carries; it has no concept of
`fleet:prd` at all. Same "latest wins" rule gru.md and marie.md already use elsewhere for PRD
comments superseding an earlier one: the newest comment carrying the line wins over an older
comment, which wins over the body. Reuses run_report._vision_claim's regex (tolerates markdown
heading/bold wrapping) rather than a second parser for the same field.

THE SEVERITY ESCAPE HATCH (gh#726). On a fleet-internal repo, `none (maintenance)` is the
*honest* answer for most work, so one linked candidate anywhere in the pack starves everything
else -- including, at one point, the fix for this exact starvation (gh#726 itself was in its own
`dropped` list). A `none (maintenance)` candidate that also carries the `fleet:severity-live`
label (an active, ongoing failure, set by hand while it is still occurring -- never auto-detected,
and deliberately not `fleet:priority-high`, which most of this board already carries) survives
the crowding-out drop. Every other rule is unchanged: a candidate with no `labels` key at all
behaves exactly as before this existed.

Pure core (`classify_candidate`/`gate_candidates`), thin CLI (`main`) -- same split as
claim_history.py and cost_bridge.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_report  # noqa: E402

STATUS_LINKED = "linked"
STATUS_MAINTENANCE = "maintenance"
STATUS_MISSING = "missing"

# gh#726: the one escape hatch out of the crowding-out branch below -- a `none (maintenance)`
# candidate that also carries this label survives even while a linked candidate is open.
# Deliberately NOT `fleet:priority-high` (most of this board is high-tier maintenance; using
# the tier itself as the hatch would empty the gate rather than fix it) and NOT auto-detected
# from issue text -- marie/judge-judy sets it by hand while a failure is still occurring, per
# its own label description (see `_SEVERITY_LABEL_META` in board_github.py).
SEVERITY_LIVE_LABEL = "fleet:severity-live"

# Tolerate the punctuation a model actually produces: "none(maintenance)", "None (Maintenance)",
# and a trailing qualifier with no dash separator at all ("none (fleet guardrail/maintenance).",
# gh#584) -- matched as a prefix on the normalized (lowercased, whitespace-stripped) value, not
# an exact-match on a dash-delimited head, so a reason clause after the parenthetical (dashed or
# not) never defeats the match, and a real link that merely mentions "maintenance" (not starting
# with "none(") is never swallowed by it.
_MAINTENANCE_RE = re.compile(r"^none\(.*maintenance.*?\)")


def _normalize(value: str) -> str:
    return "".join(value.lower().split())


def classify_candidate(body: str | None, comments: list[dict] | None) -> tuple[str, str | None]:
    """(status, raw Vision-link value) for one candidate.

    `comments` should be in `createdAt` order (ascending), same shape `gh issue list --json
    number,...,comments` returns. Newest comment carrying a `Vision-link:` line wins over an
    older one, which wins over the body -- a re-scored PRD supersedes what it superseded.
    """
    for comment in reversed(comments or []):
        claim = run_report._vision_claim(comment.get("body") or "")
        if claim is not None:
            return _classify_value(claim)
    claim = run_report._vision_claim(body or "")
    if claim is not None:
        return _classify_value(claim)
    return STATUS_MISSING, None


def _classify_value(raw: str) -> tuple[str, str]:
    # A real link is often written "Stripe MRR -- it puts the number in front of every member"
    # (#513's own example); a maintenance value is only ever "none (...maintenance...)", so a
    # prefix match on the normalized value distinguishes them without depending on a dash
    # separator being present at all (gh#584).
    if _MAINTENANCE_RE.match(_normalize(raw)):
        return STATUS_MAINTENANCE, raw
    return STATUS_LINKED, raw


def _label_names(labels) -> list[str]:
    out = []
    for lab in labels or []:
        out.append(lab.get("name", "") if isinstance(lab, dict) else str(lab))
    return out


def gate_candidates(candidates: list[dict]) -> dict:
    """candidates: [{"number": int, "body": str, "comments": [...]}, ...], already in the
    order gru.md step 2b/2c produced (tier, then oldest-createdAt-first within a tier).
    `labels` is optional (gh#726) -- a candidate dict with no `labels` key at all behaves
    exactly as it did before this key existed: no escape hatch, byte-identical output.

    Returns {"eligible": [numbers, in the same relative order], "dropped": [{"number",
    "reason"}, ...]} -- gh#525 AC3: every drop is named, never a silent absence.
    """
    classified = [
        (c["number"], *classify_candidate(c.get("body"), c.get("comments")), c.get("labels"))
        for c in candidates
    ]
    linked_numbers = [n for n, status, _, _ in classified if status == STATUS_LINKED]
    any_linked = bool(linked_numbers)

    eligible: list[int] = []
    dropped: list[dict] = []
    for number, status, raw, labels in classified:
        if status == STATUS_LINKED:
            eligible.append(number)
        elif status == STATUS_MAINTENANCE:
            # gh#726: an active, ongoing failure survives the crowding-out drop even while a
            # linked-KR candidate is open elsewhere in the pack -- everything else about
            # `none (maintenance)` is unchanged.
            if any_linked and SEVERITY_LIVE_LABEL not in _label_names(labels):
                dropped.append({
                    "number": number,
                    "reason": (
                        "none (maintenance), but a linked-KR candidate is open: "
                        f"#{linked_numbers[0]}"
                        + (f" (+{len(linked_numbers) - 1} more)" if len(linked_numbers) > 1 else "")
                    ),
                })
            else:
                eligible.append(number)
        else:
            dropped.append({
                "number": number,
                "reason": "no Vision-link line (neither a real link nor explicit "
                          "'none (maintenance)')",
            })
    return {"eligible": eligible, "dropped": dropped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Gate marie-ranked candidates on gh#525's Vision-link eligibility rule. "
                     "Reads --items JSON (same candidates gru.md step 2b/2c already pulled, "
                     "each with number/body/comments), writes {eligible, dropped} to stdout.")
    ap.add_argument("--items", required=True,
                     help="JSON list: [{\"number\":n,\"body\":\"...\",\"comments\":[...]}, ...]")
    a = ap.parse_args(argv)

    candidates = json.loads(a.items)
    print(json.dumps(gate_candidates(candidates)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
