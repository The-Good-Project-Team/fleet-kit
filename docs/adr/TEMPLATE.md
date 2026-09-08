# ADR <item>: buy vs. build -- <affordance or interaction>

Copy this file to `docs/adr/<item>.md`. One ADR per world-class item's buy-vs-build spike
(`docs/quality-standard.md` §0 step 5, step 7). Fill in every section before Reif's design
approval ask is filed -- the ADR is part of what he approves.

## Status

Proposed | Accepted

## Context

What this decision is for, in one or two sentences. Link the parity matrix this was drawn
from: `docs/design/<item>/parity-matrix.md`.

## Candidates considered

Every candidate column from the parity matrix, one line each:

- **<candidate 1>** -- what it covers, what it doesn't, ★ stars / last release.
- **<candidate 2>** -- same.
- **Hand-rolled** -- always a candidate; what it would cost to build and maintain ourselves.

## Decision

Which one was picked.

## Reason

The single named reason -- not a paragraph of tradeoffs. If more than one reason mattered,
name the one that would have changed the decision on its own.

## Consequences

What this choice commits us to (a dependency, a license, a maintenance cost) -- one or two
sentences.
