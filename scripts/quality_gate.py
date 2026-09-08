#!/usr/bin/env python3
"""quality_gate -- is this candidate specified well enough to build? fk#649 + fk#651.

THE GAP THIS CLOSES. Reif, 2026-09-07: "lots of busy work in terms of PRs, but for some reason
the best product does not get created ... I'd rather us push less code but better features."
docs/quality-standard.md answered with a quality dial and testable acceptance criteria, but
nothing enforced either: gru kept claiming from priority labels alone, so 105 product PRs
merged in one day against issues with no stated bar and no checkable criteria.

THE RULE. A candidate is eligible for gru only if BOTH hold:
  1. it carries exactly one `quality:ship-it` / `quality:solid` / `quality:world-class` label
     (marie sets it when she writes the PRD, marie.md Part C4; Reif can move it any time);
  2. its newest PRD comment (or the body, if no comment has criteria) has at least one
     acceptance criterion written as Given / When / Then -- a sentence a reviewer can answer
     yes or no to. "Handles errors gracefully" never passes this gate.
A `quality:world-class` item is eligible only if that criterion set is its research pass
(the comment names `References:` and a design-spec approval ask) OR a comment says
`Design approved:` -- nobody builds the feel before Reif has approved the design
(quality-standard.md section 0, steps 1-7).

Same shape and split as vision_link_gate.py: pure core (`classify_candidate`,
`gate_candidates`), thin CLI (`main`) reading `--items '[{"number","labels","body","comments"}]'`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

QUALITY_LABELS = ("quality:ship-it", "quality:solid", "quality:world-class")
WORLD_CLASS = "quality:world-class"

# One criterion: Given ... When ... Then ..., on one line or across up to three lines, any
# markdown wrapping (bold, list bullets, numbering).  Case-insensitive.
_GWT_RE = re.compile(r"\bgiven\b[\s\S]{1,600}?\bwhen\b[\s\S]{1,600}?\bthen\b", re.IGNORECASE)
_REFERENCES_RE = re.compile(r"^\W*references?\s*:", re.IGNORECASE | re.MULTILINE)
_DESIGN_APPROVED_RE = re.compile(r"^\W*design approved\s*:", re.IGNORECASE | re.MULTILINE)


def _label_names(labels) -> list[str]:
    out = []
    for lab in labels or []:
        out.append(lab.get("name", "") if isinstance(lab, dict) else str(lab))
    return out


def count_gwt(text: str | None) -> int:
    return len(_GWT_RE.findall(text or ""))


def _criteria_text(body: str | None, comments: list[dict] | None) -> str | None:
    """Newest comment carrying a Given/When/Then criterion wins over the body."""
    for comment in reversed(comments or []):
        text = comment.get("body") or ""
        if count_gwt(text):
            return text
    if count_gwt(body):
        return body
    return None


def classify_candidate(labels, body: str | None, comments: list[dict] | None) -> tuple[bool, str]:
    """(eligible, reason)."""
    quality = [l for l in _label_names(labels) if l in QUALITY_LABELS]
    if not quality:
        return False, "no quality: label (ship-it / solid / world-class); marie sets it in the PRD"
    if len(quality) > 1:
        return False, f"more than one quality label: {', '.join(quality)}"
    text = _criteria_text(body, comments)
    if text is None:
        return False, "no Given/When/Then acceptance criterion in the PRD comment or body"
    if quality[0] == WORLD_CLASS:
        all_text = "\n".join([body or ""] + [c.get("body") or "" for c in comments or []])
        if _DESIGN_APPROVED_RE.search(all_text):
            return True, "world-class, design approved"
        if _REFERENCES_RE.search(text):
            return True, "world-class, research-pass slice"
        return False, ("world-class with no `Design approved:` comment and no `References:` in the "
                       "criteria; only the research pass is buildable before Reif approves the design")
    return True, f"{quality[0]}, {count_gwt(text)} testable criteria"


def gate_candidates(items: list[dict]) -> dict:
    eligible, dropped = [], []
    for item in items:
        ok, reason = classify_candidate(item.get("labels"), item.get("body"), item.get("comments"))
        if ok:
            eligible.append(item["number"])
        else:
            dropped.append({"number": item["number"], "reason": reason})
    return {"eligible": eligible, "dropped": dropped}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--items", required=True, help="JSON list of {number, labels, body, comments}")
    a = p.parse_args(argv)
    print(json.dumps(gate_candidates(json.loads(a.items))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
