#!/usr/bin/env python3
"""plan_rank -- does this candidate serve one of the plan's current named bets? gh#572.

THE GAP THIS CLOSES. gru.md step 2 runs needs-human-op, dead-end (gh#64), Vision-link
(gh#525) and quality (fk#649/#651) filters, then step 3 packs whatever survives in
tier-then-createdAt order. Every survivor is treated as equally eligible -- nothing anywhere
in that chain knows which candidates are the plan's current bets (#570/#571's
`docs/plan/<instance>.md`), because no step reads a plan. Without this, #570/#571 are purely
informational: gru keeps building whatever it would have built anyway.

THE RULE (this issue's Goal, verbatim): gru prefers a candidate named as a current bet in
`docs/plan/<instance>.md` over an equally-eligible candidate that is not. This is a preference
TIER applied to survivors of every existing filter -- it never makes a candidate ineligible,
and it never writes or revises the plan (#570/#571 own that). A repo with no plan file is a
supported, tested state: ranking degrades to today's PR#536 order, unchanged.

PROVISIONAL CONTRACT (this issue's own open question -- #570 owns the real schema, not yet
landed, so this is deliberately the simplest form that satisfies AC1, not the final word):
  - a `## Bets` (or `# Bets` / `### Bets`, any heading level, case-insensitive) heading marks
    the start of the bets section; it runs to the next heading of the same or shallower level,
    or EOF.
  - every non-blank line in that section is one bet; a bet names an issue by containing a
    `#<digits>` token anywhere in the line (a list marker or numbering prefix is stripped, the
    rest of the line is kept as the bet's display text for gru's report -- AC5).
  - no `## Bets` heading anywhere in the file, or the file can't be read at all, is
    MALFORMED (AC4): ranking degrades to unchanged order, and exactly one diagnostic line
    naming the file and the problem goes to stderr -- never a traceback.
  - a `## Bets` heading whose lines carry no `#<digits>` token at all is NOT malformed -- it's
    a half-written, prose-only plan (AC3): ranking degrades to unchanged order silently, no
    diagnostic, exit 0.
#570 should conform its own writer to this contract rather than the reverse (per this issue's
own instruction), or this module's parsing updates to match whatever #570 actually ships.

`<instance>` resolves from `$FLEET_INSTANCE_NAME`, falling back to `"default"` -- the same
fallback entrypoint.sh's own crontab-forwarding comment already uses for this variable. A
trailing `-green`/`-blue` deploy-slot suffix is stripped first (fk#559 VP review fix 2):
`deploy.sh:197` bakes the container's `-green` name into `FLEET_INSTANCE_NAME` at `podman run`
time and the later cutover rename to the live name never restarts the container to pick up a
new env, so the live value on a deployed box is permanently `<instance>-green` (confirmed
2026-09-11: `fleet-kit-server-fleet-green`). Nobody hand-writes a plan file at
`docs/plan/<instance>-green.md`.

Pure core (`parse_bets`, `issue_bet_map`, `rank_candidates`), thin CLI (`main`) -- same split
as vision_link_gate.py and quality_gate.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

_BETS_HEADING_RE = re.compile(r"^(#{1,6})\s*bets\s*$", re.IGNORECASE | re.MULTILINE)
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
_ISSUE_TOKEN_RE = re.compile(r"#(\d+)\b")
_LIST_MARKER_RE = re.compile(r"^[-*]\s*")
_NUMBER_MARKER_RE = re.compile(r"^\d+[.)]\s*")
_SLOT_SUFFIX_RE = re.compile(r"-(?:green|blue)$")


def resolve_instance() -> str:
    """`$FLEET_INSTANCE_NAME` with any deploy-slot suffix stripped -- see module docstring."""
    name = os.environ.get("FLEET_INSTANCE_NAME") or "default"
    return _SLOT_SUFFIX_RE.sub("", name)


def plan_path_for_instance(instance: str | None = None, root: Path | None = None) -> Path:
    root = root or REPO_ROOT
    instance = instance or resolve_instance()
    return root / "docs" / "plan" / f"{instance}.md"


def _find_bets_section(text: str) -> str | None:
    """Raw text of the `## Bets` section (its heading line excluded), or None if no such
    heading exists anywhere in the document -- AC4 territory."""
    m = _BETS_HEADING_RE.search(text)
    if not m:
        return None
    level = len(m.group(1))
    start = m.end()
    end = len(text)
    for other in _ANY_HEADING_RE.finditer(text, start):
        if len(other.group(1)) <= level:
            end = other.start()
            break
    return text[start:end]


def parse_bets(text: str) -> tuple[list[dict], str | None]:
    """(bets, diagnostic). Each bet is {"text": <display text>, "issues": [ints]}.

    `diagnostic` is set only when no `## Bets` heading could be found at all (AC4); a heading
    with prose-only lines (no `#<digits>` anywhere) returns an empty-issues bet list and no
    diagnostic -- AC3, a supported degrade, not an error.
    """
    section = _find_bets_section(text)
    if section is None:
        return [], "no '## Bets' heading found"
    bets: list[dict] = []
    for line in section.splitlines():
        raw = line.strip()
        if not raw:
            continue
        raw = _LIST_MARKER_RE.sub("", raw)
        raw = _NUMBER_MARKER_RE.sub("", raw).strip()
        if not raw:
            continue
        issues = [int(n) for n in _ISSUE_TOKEN_RE.findall(raw)]
        bets.append({"text": raw, "issues": issues})
    return bets, None


def load_bets(path: Path) -> tuple[list[dict], str | None]:
    """(bets, diagnostic) for a plan file at `path`.

    No file at all is AC2 -- fully supported, `diagnostic` is None, `bets` is empty. An
    unreadable file (permissions, bad encoding, a directory where a file was expected) is
    treated the same as a missing `## Bets` heading -- AC4, never a raised exception.
    """
    if not path.exists():
        return [], None
    try:
        text = path.read_text()
    except OSError as exc:
        return [], f"plan_rank: {path}: could not read plan file ({exc})"
    bets, diagnostic = parse_bets(text)
    if diagnostic:
        return bets, f"plan_rank: {path}: {diagnostic}"
    return bets, None


def issue_bet_map(bets: list[dict]) -> dict[int, str]:
    """issue number -> the (first) bet's display text that names it. First-named wins, same
    "earlier entry wins a same-key conflict" default `dict.setdefault` already gives."""
    mapping: dict[int, str] = {}
    for bet in bets:
        for n in bet["issues"]:
            mapping.setdefault(n, bet["text"])
    return mapping


def rank_candidates(candidates: list[int], bet_map: dict[int, str]) -> list[int]:
    """Stable-partition `candidates`: those named by a current bet first (input order
    preserved among them), then everyone else (input order preserved among them).

    An empty `bet_map` (no plan file, AC2; a plan with no bets named, AC3) returns
    `candidates` unchanged -- byte-identical to the input, never a re-sort of any kind.
    """
    if not bet_map:
        return list(candidates)
    bet_named = [c for c in candidates if c in bet_map]
    rest = [c for c in candidates if c not in bet_map]
    return bet_named + rest


def rank(candidates: list[int], plan_path: Path | None = None) -> dict:
    """End-to-end: load the plan for `plan_path` (default: this instance's), rank
    `candidates`, print any AC4 diagnostic to stderr. Never raises -- a malformed or absent
    plan always degrades to unchanged order plus exit 0, per AC2/AC3/AC4."""
    path = plan_path or plan_path_for_instance()
    bets, diagnostic = load_bets(path)
    if diagnostic:
        print(diagnostic, file=sys.stderr)
    bet_map = issue_bet_map(bets)
    ranked = rank_candidates(candidates, bet_map)
    return {
        "ranked": ranked,
        "bet_by_issue": {str(n): text for n, text in bet_map.items() if n in candidates},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Prefer candidates named as a current bet in docs/plan/<instance>.md "
                     "over equally-eligible candidates that aren't. Reads --items JSON (the "
                     "numbers gru.md step 2's filters already produced, in their current "
                     "order), writes {ranked, bet_by_issue} to stdout. Never errors: an "
                     "absent or malformed plan degrades to the input order unchanged (exit 0).")
    ap.add_argument("--items", required=True, help="JSON list of candidate issue numbers")
    ap.add_argument("--plan-path", help="override docs/plan/<instance>.md "
                                         "(default: resolved from $FLEET_INSTANCE_NAME)")
    a = ap.parse_args(argv)

    candidates = json.loads(a.items)
    plan_path = Path(a.plan_path) if a.plan_path else None
    print(json.dumps(rank(candidates, plan_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
