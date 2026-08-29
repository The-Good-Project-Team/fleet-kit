#!/usr/bin/env python3
"""stream_log.py -- turn claude -p --output-format stream-json events into readable log lines,
in real time, as the pass runs. Reads stream-json from stdin, writes one human-readable line
per event to stdout AND passes the final `result` event's JSON through unchanged on its own
last line (pass_accounting.py's existing text/usage split still works on that line).

WHY THIS EXISTS: Reif, 2026-08-21: "the actual run log ... should log as much as it can, as
logs are cheap -- starting run now as gru, thinking about x, starting new minions, etc." The
wrapper (run_member.sh) previously only logged pass-start and pass-end -- everything in
between was invisible until the whole pass finished and dumped one JSON blob. stream-json
gives real per-turn events (assistant text/thinking, tool calls, tool results, subagent
spawns) as they happen; this is what actually makes a member's log legible DURING a long pass,
not just after it -- useful for dumbledore's rot-hunt (a stuck pass looks different from a
quiet one) and for a human tailing the log live.

Usage: claude -p ... --output-format stream-json --verbose \
         | stream_log.py --result-out /tmp/result.json >> member.log

--result-out writes the final `result` event's JSON to a separate file, UNMODIFIED, always --
every key (including `result`'s own text) is passed through exactly as the provider returned
it, so pass_accounting.py's `split()` still expects exactly one JSON blob, same as it always
has with --output-format json.

gh#167: the stream-json protocol's own `result` field is Claude Code's last-assistant-turn
text ONLY, by design -- not something fleet-kit computes. If a pass writes its full
Report:/Outcome:/Evidence: contract block, then makes one more turn (e.g. a trailing tool call
followed by a short wrap-up sentence), that wrap-up -- not the report -- becomes `result`, and
run_report.py never sees the real report at all. Confirmed live, fleet-wide (jefe, the-fixer,
datta, dont-shoot-the-messenger): 12 of 13 `reported_nothing` rows in one window had every
contract field null despite real work being reported seconds before the pass's actual last
turn.

This script does NOT try to recover from that loss by rewriting `result` -- three attempts at
exactly that (2026-08-29T04:33, T08:33, T09:34) were each BLOCKed by fleet-code-review for a
narrower version of the same flaw: any heuristic that resurrects an earlier text block as the
"real" report can't distinguish a genuine trailing wrap-up from a pass that wrote a report,
then in its very next turn discovered a problem and reverted -- and silently substituting a
since-invalidated report is worse than the visible `reported_nothing` it would replace. The
actual fix for the loss lives in persona_law.md (added alongside this change): stop after your
report block, no trailing turn. What this script does instead is DETECT the gh#167 shape (see
`_detect_trailing_loss`) and print a WARNING log line naming it, so a human or dumbledore's own
log read can find residual occurrences (`grep 'gh#167 trailing-turn'`) without any risk of
fabricating fleet.db/runs.jsonl data.
"""
from __future__ import annotations

import argparse
import json
import re
import sys


def _text_preview(text: str, limit: int = 500) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[:limit] + "…"


# Tools whose payload IS code/file content -- for these, log what happened (path + shape),
# never the content itself. Reif, 2026-08-23: "I want to know what it did, not the actual
# code" -- the file is already on disk at that path, a human (or dumbledore) can open it;
# the log's job is to say "Edit ran on x.py", not carry a second copy of the diff.
_CODE_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _describe_tool_call(name: str, inp: dict) -> str:
    if name in _CODE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or "?"
        if name == "Write":
            size = len(str(inp.get("content", "")))
            return f"{path} (write, {size} chars)"
        if name == "Edit":
            old_lines = str(inp.get("old_string", "")).count("\n") + 1
            return f"{path} (edit, ~{old_lines} line block)"
        return f"{path} (notebook edit)"
    if "command" in inp:
        return _text_preview(str(inp["command"]))
    if "file_path" in inp:
        return str(inp["file_path"])
    if "pattern" in inp:
        extra = f" in {inp['path']}" if inp.get("path") else ""
        return f"pattern={inp['pattern']!r}{extra}"
    if "prompt" in inp:
        return _text_preview(str(inp["prompt"]))
    if "description" in inp:
        return str(inp["description"])
    return ""


def render_event(evt: dict) -> str | None:
    etype = evt.get("type")

    if etype == "system" and evt.get("subtype") == "init":
        model = evt.get("model", "?")
        tools = evt.get("tools") or []
        return f"session init -- model={model} tools={len(tools)} cwd={evt.get('cwd','?')}"

    if etype == "assistant":
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                return f"thinking: {_text_preview(block['text'])}"
            if btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input") or {}
                hint = _describe_tool_call(name, inp)
                return f"tool call: {name}" + (f" -- {hint}" if hint else "")

    if etype == "user":
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                is_err = block.get("is_error", False)
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                preview = _text_preview(str(content or ""))
                return f"tool result ({'ERROR' if is_err else 'ok'}): {preview}"

    if etype == "system" and evt.get("subtype") == "hook_started":
        return f"hook: {evt.get('hook_name', '?')} started"

    if etype == "result":
        # Final summary -- rendered as a readable line here, AND the raw JSON is re-emitted
        # verbatim by main() below so pass_accounting.py's existing parser still works.
        turns = evt.get("num_turns", "?")
        cost = evt.get("total_cost_usd")
        cost_s = f"${cost:.3f}" if isinstance(cost, (int, float)) else "?"
        return f"pass result: {evt.get('subtype','?')} -- {turns} turns, {cost_s}, stop={evt.get('stop_reason','?')}"

    # Unrecognized event types (rate_limit_event, other hook subtypes, etc.) are skipped, not
    # errored on -- a future SDK field addition must never crash this filter mid-stream.
    return None


# Mirrors run_report.py's own Outcome:/Evidence: matchers just enough to detect "this text
# block contains the real contract, not just a stray mention of it" -- deliberately not
# imported, so this script has no dependency on run_report.py's internals and can't be broken
# by an unrelated change there.
_OUTCOME_RE = re.compile(r"^[ \t]*[*_]{0,2}Outcome[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
_EVIDENCE_RE = re.compile(r"^[ \t]*[*_]{0,2}Evidence[*_]{0,2}[ \t]*:[ \t]*[*_]{0,2}[ \t]*(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE)

# A recital of the contract template itself ("Write it as:\nOutcome: <one line>\nEvidence:
# <one line>") satisfies both regexes above without being a real report -- charters spell the
# template out in exactly this bracketed-placeholder shape, so this is the realistic false
# positive, not a contrived one.
_PLACEHOLDER_RE = re.compile(r"^<.*>$")


def _looks_like_report(text: str) -> bool:
    """A bare Outcome: line is too weak a signal on its own -- a draft aside ("Outcome: (draft,
    ignore)") or a recital of this very contract template ("Write it as:\\nOutcome: <one
    line>") both match it without being a real report. Requiring BOTH Outcome: and Evidence:
    lines mirrors the actual contract (persona_law.md #10b requires both together); rejecting
    angle-bracket placeholder content additionally defeats a template quote, which a plain
    both-lines check does not."""
    outcome_m = _OUTCOME_RE.search(text)
    evidence_m = _EVIDENCE_RE.search(text)
    if not (outcome_m and evidence_m):
        return False
    if _PLACEHOLDER_RE.match(outcome_m.group(1)) or _PLACEHOLDER_RE.match(evidence_m.group(1)):
        return False
    return True


# gh#167's own failure shape is exactly one tool round-trip between the real report and the
# trailing wrap-up that overwrote it -- a handful of raw stream-json events (assistant
# tool_use, user tool_result, at most one more of each). "Continues working, hits a blocker,
# reverts the change" -- the fabrication risk fleet-code-review flagged on PR #175's first cut
# -- spans many more real tool calls than that for any actual edit/investigation. This bound is
# deliberately generous (covers several tool calls, not just one) while still being orders of
# magnitude below what sustained work between an early draft and a final abandonment would
# produce, so it discriminates the two shapes without needing to parse turn semantics.
_MAX_TRAILING_EVENT_GAP = 8


def _detect_trailing_loss(result_line: str | None, assistant_texts: list[tuple[int, str]]) -> str | None:
    """gh#167, detect-only after fleet-code-review BLOCKed three straight attempts at
    *rewriting* `result` (2026-08-29T04:33, T08:33, T09:34) -- each narrower scoping still left
    a residual path where a real report gets silently overwritten by a stale/since-reverted
    draft, which is worse than the visible `reported_nothing` it replaces (a member that writes
    a report, then in its very next turn discovers the fix regressed something and reverts,
    produces exactly the same "report-shaped block immediately followed by a short non-report
    wrap-up" event shape as the trailing-turn bug -- no gap/adjacency heuristic can tell those
    two apart from the stream alone).

    So this never touches `result`: `pass_accounting.py`/`run_report.py` see exactly what the
    provider returned, always, with zero fabrication risk. What it DOES do is name the loss --
    when the immediately-preceding text block looks like a real report and the final block does
    not, close together in the raw stream, that's the gh#167 shape, and this prints a WARNING
    log line so a human or dumbledore's own log read can find it (`grep 'gh#167 trailing-turn'`)
    without silently trusting a resurrected value. The real fix for the loss itself is
    persona_law.md's own rule (added alongside this change): stop after your report block, no
    trailing turn -- this function is a visibility backstop for passes that don't yet comply,
    not a substitute for that rule."""
    if not result_line or len(assistant_texts) < 2:
        return None
    try:
        obj = json.loads(result_line)
    except json.JSONDecodeError:
        return None
    (final_idx, final_text), (prev_idx, prev_text) = assistant_texts[-1], assistant_texts[-2]
    if _looks_like_report(final_text):
        return None
    if not _looks_like_report(prev_text):
        return None
    if final_idx - prev_idx > _MAX_TRAILING_EVENT_GAP:
        return None
    if obj.get("result", "").strip() == prev_text.strip():
        return None
    return prev_text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-out", help="write the final result event's raw JSON here")
    args = ap.parse_args()

    result_line = None
    assistant_texts: list[tuple[int, str]] = []
    for event_idx, raw in enumerate(sys.stdin):
        raw = raw.strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a torn/partial line must never kill a live pass's logging
        if evt.get("type") == "result":
            result_line = raw
        if evt.get("type") == "assistant":
            for block in (evt.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    assistant_texts.append((event_idx, block["text"]))
        rendered = render_event(evt)
        if rendered:
            print(rendered, flush=True)

    lost = _detect_trailing_loss(result_line, assistant_texts)
    if lost is not None:
        print("WARNING: gh#167 trailing-turn report loss detected -- a report-shaped block "
              "was found immediately before the pass's real final turn, which does not look "
              "like a report. `result` is left untouched (no fabrication risk); the likely-"
              "lost text follows for a human or dumbledore to read:\n" + lost, flush=True)

    if args.result_out:
        with open(args.result_out, "w") as fh:
            # Empty (not missing) if the pass never produced a result line -- e.g. it was
            # killed mid-stream. run_member.sh treats an empty/unreadable file the same as a
            # failed pass_accounting.py parse: no usage captured, never a crash. Never rewritten
            # -- see _detect_trailing_loss's docstring for why recovery-by-rewrite was dropped.
            fh.write(result_line or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
