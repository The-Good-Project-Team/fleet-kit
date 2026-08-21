#!/usr/bin/env python3
"""The one report contract every fleet member satisfies, from three sources.

Reif, 2026-08-18: "they report standardized. everything standardized so we can modularize
stuff (reporting outputs, tokens, etc.)"

A run record has three legs, and WHO writes each leg is the whole design:

  lifecycle   start/end/duration/exit  -> written by the WRAPPER (ledger_event.py)
  tokens      turns/cost/weighted-in   -> parsed from `claude -p --output-format json`
  outcome     what it did + evidence   -> the only self-reported leg, and it is VALIDATED

The first two cannot be skipped because the member never writes them; the wrapper does, around
the member. That is the lesson of the thing this replaces: the lane_pass_log instruction was
self-reported, lived in a manual registrar nobody re-ran, and 6 of 7 lanes never once obeyed it
across 211 passes. An instruction a member can silently ignore is not a contract.

The third leg needs the member to actually say something, so the enforcement is inverted: its
ABSENCE is recorded as a status, not as silence. `reported_nothing` and `no_vision_link` are
outcomes you can query and count. Neither FAILS the run -- a member must not be rewarded for
skipping, and must not be killed for honest maintenance work.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The vision guardrail is OPTIONAL in the kit. In the source fleet a `Vision:` score is only
# legitimate alongside a `Vision-link:` line naming which coordination link the work moves, and
# board_rice.py is the single validator both the board and every run report use. That axis is
# product-specific -- it encodes what YOUR product is for -- so the kit ships the enforcement
# hook without the scoring module. Drop a board_rice.py next to this file to turn it on.
try:
    import board_rice  # noqa: E402
    _VISION_RE = None
except ModuleNotFoundError:  # kit default: match the field, do not judge the claim
    import re as _re
    board_rice = None
    _VISION_RE = _re.compile(r"^[ \t]*Vision-link[ \t]*:[ \t]*(.+?)[ \t]*$",
                             _re.MULTILINE | _re.IGNORECASE)


def _vision_claim(text: str):
    if board_rice is not None:
        return board_rice.vision_claim(text)
    m = _VISION_RE.search(text or "")
    return m.group(1).strip() if m and m.group(1).strip() else None

_FIELD = {
    "outcome": re.compile(r"^[ \t]*Outcome[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    "evidence": re.compile(r"^[ \t]*Evidence[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    # Captured, never enforced -- a missing self-critique never changes `status` the way a
    # missing outcome does. Reif, 2026-08-21: "it should be inherent in every member to log
    # its findings -- like having a post mortem on the run." persona_law.md §11 is what tells
    # every member to write this line; this is just where it lands structurally, in the same
    # runs.jsonl every other leg of the report already lands in, so dumbledore's rot-hunt can
    # read every member's self-critique in aggregate instead of grepping N raw logs by hand.
    "self_critique": re.compile(r"^[ \t]*Self-critique[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
}

# An outcome must name something a human can open. "I looked at the dashboard" is not an
# outcome; "#2771" is. This is the same bar the board already applies to a Vision score.
_ARTIFACT = re.compile(r"(#\d+|https?://\S+|[\w./-]+\.\w+:\d+)")

STATUS_OK = "ok"
STATUS_QUIET = "quiet"
STATUS_NOTHING = "reported_nothing"
STATUS_NO_VISION = "no_vision_link"


def parse_report(text: str) -> dict:
    """Pull the FLEET-REPORT block out of a pass's output. Never raises."""
    text = text or ""
    out = {}
    for key, rx in _FIELD.items():
        m = rx.search(text)
        out[key] = m.group(1).strip() if m else None
    # Reuse board_rice's guardrail rather than a second regex: one definition of what counts
    # as a named coordination link, shared by the board and by every run.
    out["vision_link"] = _vision_claim(text)
    return out


def classify(report: dict, *, vision_required: bool) -> str:
    """The status that goes on the run record."""
    outcome = (report.get("outcome") or "").strip()
    if not outcome:
        return STATUS_NOTHING
    if outcome.upper().startswith("QUIET"):
        # A quiet pass is legitimate, but only with evidence -- otherwise it is the
        # "looked at the same dashboards and gave up" pass that rotted the board for 20 days.
        return STATUS_QUIET if (report.get("evidence") or "").strip() else STATUS_NOTHING
    if not _ARTIFACT.search(outcome) and not _ARTIFACT.search(report.get("evidence") or ""):
        return STATUS_NOTHING
    if vision_required and not report.get("vision_link"):
        return STATUS_NO_VISION
    return STATUS_OK


def build_record(*, member: str, run_id: str, kind: str, exit_code: int,
                 pass_text: str, usage: dict | None, vision_required: bool,
                 item_id: str | None = None, pr: str | None = None) -> dict:
    """One run = one record. `usage` is pass_accounting's parsed JSON, or None (mechanical)."""
    report = parse_report(pass_text)
    rec = {
        "member": member,
        "run_id": run_id,
        "kind": kind,
        "exit_code": exit_code,
        "status": classify(report, vision_required=vision_required),
        "outcome": report["outcome"],
        "evidence": report["evidence"],
        "vision_link": report["vision_link"],
        "self_critique": report["self_critique"],
        # Deterministic, not regex-parsed from prose -- the caller already knows these when it
        # writes the record (worktree_builder.sh resolves PR_NUM itself before calling this).
        # Optional: a mechanical member or an early-exit ("no unclaimed items") has neither.
        "item_id": item_id,
        "pr": pr,
    }
    u = usage or {}
    # Field names here match pass_accounting.py's split() output verbatim -- that module is the
    # ONE place that reads `claude -p --output-format json`, so every consumer of a run record
    # (fleet_db.py, fleet_view.html) reads these same names rather than each guessing at the
    # provider's raw JSON shape a second time.
    rec["tokens"] = {
        "num_turns": u.get("num_turns"),
        "stop_reason": u.get("stop_reason"),
        "cost_usd": u.get("total_cost_usd", u.get("cost_usd")),  # cost_usd: back-compat alias
        "duration_ms": u.get("duration_ms"),
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_read_input_tokens": u.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
    }
    return rec


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Build one fleet run record from a pass.")
    ap.add_argument("--member", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--kind", default="llm")
    ap.add_argument("--exit-code", type=int, default=0)
    ap.add_argument("--pass-file", help="file holding the pass's text output ('-' for stdin)")
    ap.add_argument("--usage-file", help="JSON usage record from pass_accounting")
    ap.add_argument("--vision-required", action="store_true")
    ap.add_argument("--item-id", help="board item id this pass worked, if any")
    ap.add_argument("--pr", help="PR number this pass produced, if any")
    a = ap.parse_args(argv)

    if a.pass_file == "-":
        text = sys.stdin.read()
    elif a.pass_file:
        text = Path(a.pass_file).read_text(errors="ignore")
    else:
        text = ""
    usage = None
    if a.usage_file and Path(a.usage_file).exists():
        try:
            usage = json.loads(Path(a.usage_file).read_text())
        except Exception:
            usage = None

    rec = build_record(member=a.member, run_id=a.run_id, kind=a.kind, exit_code=a.exit_code,
                       pass_text=text, usage=usage, vision_required=a.vision_required,
                       item_id=a.item_id, pr=a.pr)
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
