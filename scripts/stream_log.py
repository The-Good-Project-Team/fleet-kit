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

--result-out writes the final `result` event's JSON to a separate file -- its `result` text
field is overridden (see _rewrite_result below) when a real report was written earlier in the
stream; every other key (usage, cost, turns) is passed through unchanged, so
pass_accounting.py's `split()` still expects exactly one JSON blob, same as it always has with
--output-format json.

gh#167: the stream-json protocol's own `result` field is Claude Code's last-assistant-turn
text ONLY, by design -- not something fleet-kit computes. If a pass writes its full
Report:/Outcome:/Evidence: contract block, then makes one more turn (e.g. a trailing tool call
followed by a short wrap-up sentence), that wrap-up -- not the report -- becomes `result`, and
run_report.py never sees the real report at all. Confirmed live, fleet-wide (jefe, the-fixer,
datta, dont-shoot-the-messenger): 12 of 13 `reported_nothing` rows in one window had every
contract field null despite real work being reported seconds before the pass's actual last
turn. Fix: this script already sees every assistant text block as it streams by (that's what
the `thinking:` log lines are, per _CODE_TOOLS note above -- every text block, not literal
extended thinking); it now also keeps the full (non-preview) text of each one, and if the
LAST block containing a contract line differs from the final `result` text, that block's text
replaces `result`. Scoped narrowly on purpose: only overrides when an earlier block actually
has a contract line the final turn lacks, so a pass whose report already IS the last turn (the
common case) is byte-for-byte unaffected.
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


# Mirrors run_report.py's own Outcome: matcher just enough to detect "this text block contains
# a real contract line" -- deliberately not imported, so this script has no dependency on
# run_report.py's internals and can't be broken by an unrelated change there.
_OUTCOME_RE = re.compile(r"^[ \t]*[*_]{0,2}Outcome[*_]{0,2}[ \t]*:", re.MULTILINE | re.IGNORECASE)


def _rewrite_result(result_line: str | None, assistant_texts: list[str]) -> str | None:
    """gh#167: if a LATER turn overwrote the real report with a trailing wrap-up, restore the
    last assistant text block that actually contains an Outcome: line. No-op (returns
    result_line unchanged) whenever the final turn already is the report, or no block ever
    had one -- both the common case and the current behavior."""
    if not result_line or not assistant_texts:
        return result_line
    try:
        obj = json.loads(result_line)
    except json.JSONDecodeError:
        return result_line
    for text in reversed(assistant_texts):
        if _OUTCOME_RE.search(text):
            if obj.get("result") != text:
                obj["result"] = text
                return json.dumps(obj)
            break
    return result_line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-out", help="write the final result event's raw JSON here")
    args = ap.parse_args()

    result_line = None
    assistant_texts: list[str] = []
    for raw in sys.stdin:
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
                    assistant_texts.append(block["text"])
        rendered = render_event(evt)
        if rendered:
            print(rendered, flush=True)

    if args.result_out:
        with open(args.result_out, "w") as fh:
            # Empty (not missing) if the pass never produced a result line -- e.g. it was
            # killed mid-stream. run_member.sh treats an empty/unreadable file the same as a
            # failed pass_accounting.py parse: no usage captured, never a crash.
            fh.write(_rewrite_result(result_line, assistant_texts) or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
