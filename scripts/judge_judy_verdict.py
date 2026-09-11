#!/usr/bin/env python3
"""judge_judy_verdict -- read judge-judy's review verdict from validated JSON, not a regex
scraped over free prose (gh#806).

`claude -p --output-format json --json-schema <schema>` enforces the verdict schema at the
tool-call layer: the model's answer is forced through a schema-conforming tool call, retried
internally by the CLI before this script ever sees the output (gh#806 PRD AC3 -- a schema
mismatch on the model's first internal attempt never reaches, and never increments, judge-judy.sh's
own strike counter; a strike there means the CLI itself gave up after its own retries).

Confirmed live against the installed CLI 2026-09-11 (resolves the PRD's own UNKNOWN): the
envelope carries the validated object TWICE -- already parsed as `structured_output`, and
json-encoded a second time inside the string field `result` (the field pass_accounting.py's
`text` subcommand reads, kept for callers that only look there). Prefer `structured_output`;
fall back to a second `json.loads` of `result` for a CLI build that only sets the string form.

Usage: `claude -p ... --output-format json --json-schema "$SCHEMA" | python3 judge_judy_verdict.py`
Exit 0, `{"ok": true, "verdict": "approve"|"block", "findings": [...], "findings_text": "..."}`
  on stdout, when the object validates.
Exit 1, `{"ok": false, "reason": "<what was wrong>"}` on stdout on any failure -- the outer
  envelope wasn't JSON, neither structured_output nor a parseable result was present, or the
  object failed the verdict's own shape checks. judge-judy.sh treats exit 1 as a schema-parse
  strike (gh#221) and holds the PR after MAX_PARSE_STRIKES, never leaves it unjudged (AC2).
"""
from __future__ import annotations

import json
import sys

VALID_VERDICTS = ("approve", "block")
REQUIRED_FINDING_KEYS = ("file", "line", "severity", "what_breaks")


def _findings_text(findings: list) -> str:
    lines = []
    for f in findings:
        lines.append(f"- {f.get('file', '?')}:{f.get('line', '?')} ({f.get('severity', '?')}): {f.get('what_breaks', '?')}")
    return "\n".join(lines)


def parse(raw: str) -> dict:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": f"outer --output-format json envelope did not parse: {e}"}
    if not isinstance(envelope, dict):
        return {"ok": False, "reason": f"outer envelope was not a JSON object (got {type(envelope).__name__})"}

    obj = envelope.get("structured_output")
    if obj is None:
        result = envelope.get("result")
        if not isinstance(result, str):
            return {"ok": False, "reason": "no structured_output and no string result field in envelope"}
        try:
            obj = json.loads(result)
        except json.JSONDecodeError as e:
            return {"ok": False, "reason": f"result field was not valid JSON: {e}"}

    if not isinstance(obj, dict):
        return {"ok": False, "reason": f"verdict object was not a JSON object (got {type(obj).__name__})"}

    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        return {"ok": False, "reason": f"verdict field missing or not one of {VALID_VERDICTS} (got {verdict!r})"}

    findings = obj.get("findings")
    if not isinstance(findings, list):
        return {"ok": False, "reason": f"findings field missing or not a list (got {type(findings).__name__ if findings is not None else 'missing'})"}
    for i, f in enumerate(findings):
        if not isinstance(f, dict) or not all(k in f for k in REQUIRED_FINDING_KEYS):
            return {"ok": False, "reason": f"findings[{i}] missing a required key ({'/'.join(REQUIRED_FINDING_KEYS)})"}

    # A block with no findings is as useless as no verdict at all -- it posts a hard,
    # required-check-blocking failure with nothing a human or the-fixer can act on (issue
    # #3170). Fold this into the same schema-violation path rather than a second check
    # downstream: judge-judy.sh only ever needs one "was this a valid answer" gate.
    if verdict == "block" and not findings:
        return {"ok": False, "reason": "verdict=block with an empty findings list -- nothing a human can act on"}

    return {"ok": True, "verdict": verdict, "findings": findings, "findings_text": _findings_text(findings)}


def main(argv=None) -> int:
    raw = sys.stdin.read()
    out = parse(raw)
    json.dump(out, sys.stdout)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
