#!/usr/bin/env python3
"""lane_registry_lanes -- read a target repo's own `REGISTRY` lane names without executing it
(gh#638).

`scripts/run_member.sh`'s nerd lane-validation rejects any `lane=<name>` dispatch outside the
current target's real lanes BEFORE any lane-specific work runs (gh#374). Until now that list
was one hardcoded fleet-kit constant, so a target like philanthropy whose own
`src/philanthropy/ops/lane_kpis.py` `REGISTRY` legitimately defines lanes fleet-kit has never
heard of (`claim`, `audience`, `coordination`) had every dispatch to them rejected before it
ever started (gh#638).

`run_member.sh` is the ONE shared, human-maintained script every `FLEET_REPO` target execs
through -- it must not `import` a product repo's own module to answer a name-only question:
that would run that repo's code (its own imports, its own side effects, its own dependency
versions) inside this shared script's process for a single lookup, exactly the "fragile
coupling" gh#638 itself warned against. Parsing the file's AST and reading only the `REGISTRY`
dict's string-literal keys gets the same names without ever executing a line of it.

Usage: lane_registry_lanes.py <path-to-lane_kpis.py>
Prints the lane names, space-separated, to stdout. Prints nothing (not an error) if the file
is missing, unparseable, or has no top-level `REGISTRY = {...}` assignment -- the caller's own
fallback to the fleet-kit list is the correct behavior for all three, not a crash.
"""
from __future__ import annotations

import ast
import sys


def registry_lanes(path: str) -> list[str]:
    try:
        tree = ast.parse(open(path, "r", encoding="utf-8").read(), filename=path)
    except (OSError, SyntaxError):
        return []

    lanes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REGISTRY" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                lanes.add(key.value)
    return sorted(lanes)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(0)
    print(" ".join(registry_lanes(sys.argv[1])))
