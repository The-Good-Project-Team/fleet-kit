#!/usr/bin/env python3
"""pass_accounting — split a `claude -p --output-format json` blob into text + usage.

Provider built-in, not hand-rolled: `claude -p --output-format json` already returns real
per-call cost/tokens/turns (total_cost_usd, usage.{input,output,cache_read_input,
cache_creation_input}_tokens, num_turns, stop_reason, duration_ms) -- confirmed live against a
real call, no OTel/gateway/enterprise config needed, works out of the box with any account.
This file's only job is separating that JSON's two halves for callers that need them
separately: the free-text `result` (what worktree_builder.sh greps for a PR URL, what
run_report.py's Outcome:/Evidence: regexes read) and the usage numbers (what fleet_db.py
stores). Every field this reads is passed through unchanged -- no derived/estimated numbers.

Usage: `claude -p ... --output-format json | python3 pass_accounting.py text`  (prints `result`,
or the raw input verbatim if it wasn't valid JSON -- so a caller piping through this never loses
output just because parsing failed)
       `claude -p ... --output-format json | python3 pass_accounting.py usage > usage.json`
"""
from __future__ import annotations

import json
import sys


def split(raw: str) -> tuple[str, dict]:
    """Returns (text, usage_dict). usage_dict is {} if raw wasn't parseable JSON --
    the caller still gets `raw` back as text so a malformed/truncated response doesn't
    silently swallow output, only the cost accounting for that one pass."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw, {}
    text = obj.get("result", "")
    usage_block = obj.get("usage") or {}
    usage = {
        "total_cost_usd": obj.get("total_cost_usd"),
        "num_turns": obj.get("num_turns"),
        "stop_reason": obj.get("stop_reason"),
        "duration_ms": obj.get("duration_ms"),
        "input_tokens": usage_block.get("input_tokens"),
        "output_tokens": usage_block.get("output_tokens"),
        "cache_read_input_tokens": usage_block.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage_block.get("cache_creation_input_tokens"),
    }
    return text, usage


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in ("text", "usage"):
        print("usage: pass_accounting.py text|usage  (reads claude -p --output-format json from stdin)",
              file=sys.stderr)
        return 2
    raw = sys.stdin.read()
    text, usage = split(raw)
    if argv[0] == "text":
        sys.stdout.write(text)
    else:
        json.dump(usage, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
