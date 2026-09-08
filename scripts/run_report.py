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

An empty third leg has more than one cause, though, and they are not the same event:
`reported_nothing` is a pass that ran to completion and said nothing, but `budget_declined`
(exit_code 3, account_pool.sh's ALL_ACCOUNTS_EXHAUSTED) is a pass that never got to run at
all -- zero tokens spent -- and `timed_out` (exit_code 124) is a pass killed mid-run. Collapsing
all three into one status is how #3015 happened: a token_efficiency.py throttle decision
counted zero-spend budget declines as evidence of a barren member.
"""
from __future__ import annotations

import json
import re
import sys
import time
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
    _VISION_RE = _re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Vision-link[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$",
                             _re.MULTILINE | _re.IGNORECASE)


def _vision_claim(text: str):
    if board_rice is not None:
        return board_rice.vision_claim(text)
    m = _VISION_RE.search(text or "")
    return m.group(1).strip() if m and m.group(1).strip() else None

_FIELD = {
    # [*_]{0,2} on both sides of the label (and once more after the colon) tolerates a
    # markdown-emphasis wrapper in either order a model tends to produce it -- `**Outcome:**
    # text` (stars close after the colon) or `**Outcome**: text` (stars close before it).
    # gh#76: the un-wrapped version masked 283/322 (88%) of dont-shoot-the-messenger's runs
    # as `reported_nothing` when the pass had done real, evidenced work.
    # #{0,6} tolerates the same field written as a markdown heading (`## Outcome:` /
    # `### **Evidence:**`) -- gh#135: dont-shoot-the-messenger (haiku) reliably opens its
    # report with `## Report **BOTTOM LINE:**` instead of the literal `Report:` line, the
    # same over-decoration habit #76 already fixed for bold-wrapping, one markdown construct
    # further. A field wrapped in a heading is still that field; the label text is what
    # matters, not whether the model dressed it up as a section title.
    "outcome": re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Outcome[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    "evidence": re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Evidence[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    # Captured, never enforced -- a missing self-critique never changes `status` the way a
    # missing outcome does. Reif, 2026-08-21: "it should be inherent in every member to log
    # its findings -- like having a post mortem on the run." persona_law.md §11 is what tells
    # every member to write this line; this is just where it lands structurally, in the same
    # runs.jsonl every other leg of the report already lands in, so dumbledore's rot-hunt can
    # read every member's self-critique in aggregate instead of grepping N raw logs by hand.
    "self_critique": re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Self-critique[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    # The recursive-self-improvement leg (#83). Same treatment as self_critique -- captured,
    # never enforced, absence never changes `status`. These three lines are the only ones in
    # the report contract whose READER is the NEXT pass rather than a human: dumbledore's
    # charter requires each pass to predict, and the pass after it to return a verdict on
    # whether that prediction came true. Before this, the fields were parsed nowhere and the
    # only trace of them on disk was stream_log.py's truncated `thinking:` lines -- which are
    # intermediate reasoning, not the final answer, so the verdict had nothing to check
    # against and dumbledore's own score reasoning has cited the broken chain since 08-25.
    "prediction": re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Prediction[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    "score_now": re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Score-now[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
    "last_verdict": re.compile(r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Last-verdict[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE),
}

# THE WRITTEN REPORT, and the only multi-LINE field in the contract. Reif, 2026-08-26: "I want
# a report after each run... I paid for it after all." A pass costs real money and 20-80 turns;
# what came back was three one-line fields, and the only alternative was a 170-line raw
# transcript. Neither is a report.
#
# Every other field above is `(.+?)$` -- single line by construction, because `status` keys off
# them and a parser that swallowed paragraphs would make `Outcome:` unbounded. This one is
# deliberately different: it captures everything from `Report:` to the first line that starts
# a DIFFERENT contract field (Outcome/Evidence/Self-critique/Prediction/Score-now/
# Last-verdict/Vision-link) or the end of the output. So a member writes prose -- paragraphs,
# bullets, numbers -- and it survives intact into runs.jsonl.
#
# Captured, NEVER enforced: like self_critique, a missing report must not change `status`. A
# pass that did real work and skipped the prose is still a successful pass; making the report
# load-bearing would turn a formatting slip into a false failure, which is the exact bug §10b
# exists to prevent.
_REPORT_RE = re.compile(
    r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}Report[*_]{0,2}[ \t]*:[ \t]*\n?(.*?)"
    r"(?=^[ \t]*#{0,6}[ \t]*[*_]{0,2}(?:Outcome|Evidence|Self-critique|Prediction|"
    r"Score-now|Last-verdict|Vision-link)[*_]{0,2}[ \t]*:|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL)

# An outcome must name something a human can open. "I looked at the dashboard" is not an
# outcome; "#2771" is. This is the same bar the board already applies to a Vision score.
# gh#251: a path/PID/SHA has no GitHub-artifact shape of its own, but this fleet's own
# convention for "this is the concrete thing" is to wrap it in backticks -- so a non-empty
# backtick span counts too. A bare `` `` `` (nothing inside) still does not.
_ARTIFACT = re.compile(r"(#\d+|https?://\S+|[\w./-]+\.\w+:\d+|`[^`]+`)")

STATUS_OK = "ok"
STATUS_QUIET = "quiet"
STATUS_NOTHING = "reported_nothing"
STATUS_NO_VISION = "no_vision_link"
STATUS_BUDGET_DECLINED = "budget_declined"
STATUS_TIMED_OUT = "timed_out"
STATUS_KILLED = "killed"
STATUS_INCOMPLETE_FANOUT = "incomplete_fanout"
# gh#257 AC2/AC3: dont-shoot-the-messenger's Case 1 -- a real Report:/Outcome:/Evidence: block
# composed correctly, then overwritten by one more trailing turn (the stream-json protocol's
# `result` field is Claude Code's LAST assistant turn only, by design). stream_log.py's own
# `_detect_trailing_loss` already recognizes this shape from the raw event stream and prints a
# WARNING log line -- but a log line a human has to go grep for is not a status, and
# run_report.py's classify() never consulted it, so the run still landed reported_nothing even
# when the loss was already detected elsewhere in the pipeline. This status is set ONLY when the
# wrapper (run_member.sh) tells us, via --trailing-loss, that stream_log.py's detector actually
# fired for this run -- never inferred here from `pass_text` alone, since by the time this
# module sees `pass_text` the loss has already happened and the real report text is gone.
STATUS_REPORT_LOST = "report_lost"
# gh#145: a provisional row written before `claude -p` even runs, NOT a completion status --
# see build_started_record below. Never returned by classify(), so this is not a new status()
# a completed run can carry; it exists only to be the "started" leg fleet_stats.lost_passes()
# looks for with no matching completion row past a grace window.
STATUS_STARTED = "started"

# gh#252: a fan-out parent (the-fixer, or any member that spawns one `--item` sub-pass per
# unit of work, per docs/gru-minions.md's own reasoning) that dispatches background sub-passes
# and then ends its turn without ever writing Outcome:/Evidence: reads identically to a pass
# that ran to completion and genuinely found nothing -- but it isn't: real money was spent, and
# the dispatched items were silently orphaned when their sub-passes got killed with no record
# tying them back to a parent. Matching the dispatch invocation verbatim (`run_member.sh
# <member> --item <N>`, the exact form run_member.sh itself validates as numeric-only) is
# marie's flagged candidate for detecting this in the PRD (gh#252) -- the precise pattern is
# called out there as an open question unresolved from the repo alone, so this is the most
# literal reading of that candidate, not a final answer a human has signed off on.
#
# gh#257 AC1: datta's real dispatch invocation (datta.md:109) is a SECOND, different shape --
# `run_member.sh nerd --task "lane=<lane> — ..."`, with no `--item <N>` anywhere (nerd is
# lane-dispatched, not item-dispatched). Confirmed live 2026-08-30 07:15:59 UTC
# (run_id datta-8636-1788073921): datta spawned two real nerd sub-passes this way, then ran out
# of budget mid-poll before ever writing Outcome:/Evidence: -- dispatched_items was always []
# for this shape, so classify() fell through to reported_nothing instead of incomplete_fanout,
# identical to gh#252's the-fixer case but through a pattern the original regex never covered.
# There is no numeric item ID in this shape, so the captured token is the lane name instead.
_DISPATCH_RE = re.compile(
    r"run_member\.sh\s+\S+\s+--item\s+(\d+)"
    r"|run_member\.sh\s+\S+\s+--task\s+[\"']?lane=(\S+)")

# account_pool.sh's account_pool_run returns 3 for ALL_ACCOUNTS_EXHAUSTED: every account was
# gated before a single `claude` call was made, so this pass spent ZERO tokens.
# `timeout "$TIMEOUT" claude -p ...` (run_member.sh) returns 124 when the pass was killed
# mid-run -- it may have spent tokens, but never got to write a FLEET-REPORT block. Both cases
# produce an empty `outcome`, exactly like a pass that ran to completion and simply said
# nothing -- without exit_code, classify() cannot tell them apart (issue #3015).
# 143 = 128+15 (SIGTERM), 137 = 128+9 (SIGKILL): the pass was killed by something OUTSIDE
# itself -- in practice a deploy cutover stopping the container out from under an in-flight
# pass (`podman stop -t 10` sends SIGTERM, then SIGKILL). Found live 2026-08-26: an ad-hoc
# marie pass scoring issue complexity was SIGKILLed mid-run by auto_deploy landing #92, and
# left NO runs.jsonl record at all -- the record write happens after `claude -p` returns, so a
# killed pass simply never reached it. ~$3 of spend and 17 completed scores were invisible;
# the only trace was a log that stopped mid-sentence. A killed pass is NOT timed_out (it had
# time left) and NOT budget_declined (it was spending fine) -- it is work that was interrupted
# and is safe to re-run, which is a different operator decision from either.
_EXIT_CODE_STATUS = {3: STATUS_BUDGET_DECLINED, 124: STATUS_TIMED_OUT,
                     137: STATUS_KILLED, 143: STATUS_KILLED}


def parse_report(text: str) -> dict:
    """Pull the FLEET-REPORT block out of a pass's output. Never raises."""
    text = text or ""
    out = {}
    m = _REPORT_RE.search(text)
    # Cap at ~8k: a report is prose a human reads, not a place to paste the transcript back in.
    # Truncation is visible (the marker) rather than silent -- an invisible cut would let a
    # member think it filed something it did not.
    if m and m.group(1).strip():
        body = m.group(1).strip()
        out["report"] = body if len(body) <= 8000 else body[:8000] + "\n[... report truncated at 8000 chars]"
    else:
        out["report"] = None

    for key, rx in _FIELD.items():
        m = rx.search(text)
        out[key] = m.group(1).strip() if m else None
    # Reuse board_rice's guardrail rather than a second regex: one definition of what counts
    # as a named coordination link, shared by the board and by every run.
    out["vision_link"] = _vision_claim(text)
    # gh#252/gh#257: item IDs (`--item <N>`) or lane names (`--task "lane=<lane> ..."`) this
    # pass named in a run_member.sh dispatch line, in the order they appear -- whichever
    # alternative of _DISPATCH_RE matched. Only meaningful when `outcome` is empty (see
    # classify()) -- a pass that dispatched AND reported normally may still mention the same
    # line in its prose, which is fine, since that path never reaches STATUS_INCOMPLETE_FANOUT.
    out["dispatched_items"] = [item or lane for item, lane in _DISPATCH_RE.findall(text)]
    return out


def classify(report: dict, *, vision_required: bool, exit_code: int | None = None,
            trailing_loss: bool = False) -> str:
    """The status that goes on the run record."""
    outcome = (report.get("outcome") or "").strip()
    if not outcome:
        # A budget decline or a timeout never gets the chance to write a FLEET-REPORT block --
        # that empty outcome must not read the same as a pass that ran to completion and
        # genuinely filed nothing (#3015).
        if exit_code in _EXIT_CODE_STATUS:
            return _EXIT_CODE_STATUS[exit_code]
        # gh#252: exit_code 0 (or unknown) with an empty outcome AND evidence the pass
        # dispatched a background sub-pass it never waited on is a live real-work loss, not a
        # genuine "ran to completion and found nothing" -- distinguish it so the orphaned items
        # don't vanish into reported_nothing with no trace back to what was dispatched. Checked
        # BEFORE trailing_loss (gh#461): a pass that both wrote a real report inside
        # _detect_trailing_loss's window AND dispatched a fan-out it never waited on is still an
        # incomplete_fanout -- the dispatched sub-passes are the actionable orphan, and
        # incomplete_fanout is itself already a form of real-work loss, so report_lost must not
        # shadow it and drop orphaned_items.
        if report.get("dispatched_items"):
            return STATUS_INCOMPLETE_FANOUT
        # gh#257 AC2/AC3: the wrapper already confirmed (via stream_log.py's
        # _detect_trailing_loss) that a real report existed one turn earlier and was overwritten
        # -- this is real-work loss, distinct from both a genuine "found nothing" pass and from
        # incomplete_fanout's "never got as far as writing a report at all".
        if trailing_loss:
            return STATUS_REPORT_LOST
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


def build_started_record(*, member: str, run_id: str, kind: str = "llm",
                         item_id: str | None = None, lane: str | None = None) -> dict:
    """gh#145: the FIRST leg of a run record, written before `claude -p` is ever invoked.

    build_record's two callers (run_member.sh's normal-exit path and its SIGTERM trap,
    record_killed_pass) both write only after the pass returns control to the wrapper -- a
    pass that vanishes before either point (SIGKILL, container replacement, OOM) leaves no
    trace in runs.jsonl at all, indistinguishable from never having run. This is that trace: a
    provisional row sharing $RUN_ID with whichever completion record eventually lands (or
    never does), so fleet_stats.lost_passes() can pair the two -- "started" + a completion row
    for the same run_id is healthy; "started" with none after a grace window is a detected gap.

    Deliberately minimal: no exit_code, no tokens, no outcome/evidence -- none of that exists
    yet. Adding fields here later must not change what classify()/build_record() do with an
    ordinary completion record (that's a separate, unrelated status space -- see STATUS_STARTED).
    """
    return {
        "member": member,
        "run_id": run_id,
        "kind": kind,
        "ts": time.time(),
        "status": STATUS_STARTED,
        "item_id": item_id,
        "lane": lane,
    }


def build_record(*, member: str, run_id: str, kind: str, exit_code: int,
                 pass_text: str, usage: dict | None, vision_required: bool,
                 item_id: str | None = None, pr: str | None = None,
                 lane: str | None = None, trailing_loss: bool = False) -> dict:
    """One run = one record. `usage` is pass_accounting's parsed JSON, or None (mechanical)."""
    report = parse_report(pass_text)
    if not report.get("report") and kind != "llm" and (pass_text or "").strip():
        # fk#748, Reif: "should publish its report each run and let me see it." A shell
        # member has no prose Report: block; what it printed IS its report. Tail, capped
        # like the prose path, so the console's run panel is never blank for roomba/librarian.
        tail = (pass_text or "").strip()[-8000:]
        report["report"] = "(script output)\n" + tail
    status = classify(report, vision_required=vision_required, exit_code=exit_code,
                      trailing_loss=trailing_loss)
    rec = {
        "member": member,
        "run_id": run_id,
        "kind": kind,
        # Wall-clock time this record was WRITTEN (pass just finished) -- the only timestamp
        # this contract has ever had, and there was none until now (Reif: "I want to know what
        # time this thing ran, and how long ago that was" -- the dashboard had no field to show).
        # Epoch seconds, not ISO -- matches every other numeric field in this record and needs
        # no timezone handling on the reading side.
        "ts": time.time(),
        "exit_code": exit_code,
        "status": status,
        "outcome": report["outcome"],
        "evidence": report["evidence"],
        "vision_link": report["vision_link"],
        "self_critique": report["self_critique"],
        # The written report -- prose, multi-line, what a human actually reads after paying for
        # the pass. See _REPORT_RE for why it is the one field that spans lines.
        "report": report.get("report"),
        # #83: the compounding chain's data plane. A pass writes `prediction`; the NEXT pass
        # reads it back out of fleet.db and writes `last_verdict` about it.
        "prediction": report["prediction"],
        "score_now": report["score_now"],
        "last_verdict": report["last_verdict"],
        # Deterministic, not regex-parsed from prose -- the caller already knows these when it
        # writes the record (worktree_builder.sh resolves PR_NUM itself before calling this).
        # Optional: a mechanical member or an early-exit ("no unclaimed items") has neither.
        "item_id": item_id,
        "pr": pr,
        # Structured mirror of a dispatcher's `lane=<name>` prefix in --task (nerd is spawned
        # this way by datta). Before this field, datta's own lane-attribution had to
        # keyword-match free-text outcome/evidence against lane names -- self-reported by datta
        # as fragile and the cause of at least one real mis-attribution. Optional: only a
        # lane-dispatched pass sets it.
        "lane": lane,
        # gh#252: which item IDs this pass named in a dispatch line, when the pass never
        # reported and that's why -- null unless status is actually incomplete_fanout, so a
        # normal ok/quiet run (which may also mention a dispatch line in its prose) doesn't
        # carry a misleading orphaned_items list.
        "orphaned_items": report["dispatched_items"] if status == STATUS_INCOMPLETE_FANOUT else None,
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
    ap.add_argument("--lane", help="lane this pass was dispatched for, if any (e.g. nerd's lane=<name> --task prefix)")
    ap.add_argument("--trailing-loss", action="store_true",
                    help="gh#257: stream_log.py's _detect_trailing_loss fired for this run -- "
                         "a real report existed one turn earlier and was overwritten by a "
                         "trailing turn (gh#167's shape). Set by run_member.sh, never inferred "
                         "here from pass_text alone.")
    ap.add_argument("--started", action="store_true",
                    help="write a provisional 'started' row (gh#145), before claude -p runs -- "
                         "ignores --exit-code/--pass-file/--usage-file/--vision-required/--pr")
    a = ap.parse_args(argv)

    if a.started:
        rec = build_started_record(member=a.member, run_id=a.run_id, kind=a.kind,
                                   item_id=a.item_id, lane=a.lane)
        print(json.dumps(rec))
        return 0

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
                       item_id=a.item_id, pr=a.pr, lane=a.lane, trailing_loss=a.trailing_loss)
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
