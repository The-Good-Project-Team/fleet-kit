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

--result-out writes the final `result` event's raw JSON to a separate file, unmodified --
that file is what pass_accounting.py reads (its `split()` still expects exactly one JSON
blob, same as it always has with --output-format json; this script never changes that
contract, it just captures the one line the pass eventually produces that matches it).
"""
from __future__ import annotations

import argparse
import json
import sys


def _text_preview(text: str, limit: int = 160) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[:limit] + "…"


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
                # Keep this cheap and safe: name the tool + the one or two fields a human
                # would want at a glance, never the full payload (could be a huge diff/file).
                hint = ""
                if "command" in inp:
                    hint = _text_preview(str(inp["command"]), 100)
                elif "file_path" in inp:
                    hint = str(inp["file_path"])
                elif "prompt" in inp:
                    hint = _text_preview(str(inp["prompt"]), 100)
                elif "description" in inp:
                    hint = str(inp["description"])
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
                preview = _text_preview(str(content or ""), 120)
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-out", help="write the final result event's raw JSON here")
    args = ap.parse_args()

    result_line = None
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
        rendered = render_event(evt)
        if rendered:
            print(rendered, flush=True)

    if args.result_out:
        with open(args.result_out, "w") as fh:
            # Empty (not missing) if the pass never produced a result line -- e.g. it was
            # killed mid-stream. run_member.sh treats an empty/unreadable file the same as a
            # failed pass_accounting.py parse: no usage captured, never a crash.
            fh.write(result_line or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
