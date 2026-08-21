#!/usr/bin/env python3
"""fanout — pure headroom -> builder-count N mapping.

Provenance: genericized from nonprofit-atlas's `scripts/mac/mbuild_fanout.py` (source
directive: "N should be the number of builders you can safely spawn given current token
availability — never limit it").

Originally also carried gate-throughput backpressure (open-PR queue subtracting from
ambition), added after an incident where fanout kept spawning builders into a merge-gate
queue nothing was draining. Removed 2026-08-21 (Reif) once the underlying deploy-side clog
that caused that was fixed — the queue itself no longer backs up, so the extra dial was
dead weight. If a similar clog ever recurs, re-add backpressure at the layer that's actually
clogged (the deploy pipeline), not by throttling ambition here again.

n_from_headroom — token budget -> ambition, via a Fibonacci ladder (never a hard cap; more
headroom always maps to a higher rung, however far the ladder has to walk).

Kept pure (no network, no filesystem) so it's unit-testable without touching your CI or
GitHub. The caller (`worktree_builder.sh`'s orchestrator, or your own script) supplies the
real headroom number.

Env (read at import; see fleet.env.example): FLEET_HEADROOM_FRACTION (default 0.5) — the
fraction of reported token headroom to plan against; the rest is reserve for other
concurrent consumers of the same account (a reviewer pass, a CEO pass, an architect pass).
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator

DEFAULT_HEADROOM_FRACTION = float(os.environ.get("FLEET_HEADROOM_FRACTION", "0.5"))


def fibonacci_ladder() -> Iterator[int]:
    """Yields 1, 2, 3, 5, 8, 13, 21, 34, ... forever (deduplicated Fibonacci)."""
    a, b = 1, 2
    yield a
    while True:
        yield b
        a, b = b, a + b


def n_from_headroom(
    headroom_tokens: float,
    spend_per_build: float,
    floor: int = 1,
    headroom_fraction: float | None = None,
) -> int:
    """Largest Fibonacci rung <= floor(headroom_tokens * headroom_fraction / spend_per_build)
    "build units", never below `floor`. No upper clamp — the ladder is walked until it
    exceeds the available units, however far that goes.

    headroom_fraction defaults to DEFAULT_HEADROOM_FRACTION (FLEET_HEADROOM_FRACTION env,
    0.5) — the reserve held back for other concurrent consumers of the same token pool. Pass
    headroom_fraction=1.0 explicitly to plan against the full reported headroom.

    Raises ValueError for a non-positive spend_per_build (a caller misconfiguration, not a
    budget state — must not be swallowed into a silent 0 or 1).
    """
    if spend_per_build <= 0:
        raise ValueError(f"spend_per_build must be > 0, got {spend_per_build!r}")
    fraction = DEFAULT_HEADROOM_FRACTION if headroom_fraction is None else headroom_fraction
    effective_headroom = headroom_tokens * fraction
    units = max(0, int(effective_headroom // spend_per_build))
    n = floor
    for rung in fibonacci_ladder():
        if rung > units:
            break
        n = rung
    return max(n, floor)


def main() -> int:
    args = sys.argv[1:]
    if len(args) not in (2, 3):
        print("usage: fanout.py <headroom_tokens> <spend_per_build> [floor]", file=sys.stderr)
        return 2
    headroom_tokens = float(args[0])
    spend_per_build = float(args[1])
    floor = int(args[2]) if len(args) == 3 else 1
    try:
        n = n_from_headroom(headroom_tokens, spend_per_build, floor=floor)
        print(n)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
