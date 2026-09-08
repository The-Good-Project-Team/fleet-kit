# Parity matrix -- <item title>

Copy this file to `docs/design/<item>/parity-matrix.md` and fill it in. One row per
affordance (a thing the interaction can do, not a screen). See
[`docs/quality-standard.md`](../quality-standard.md) §0 steps 3 and 5 for what each column is
for -- this file has no column those steps do not name.

| Affordance | Reference 1 | Reference 2 | Reference 3 | Ours today | RAIL budget | Candidate 1 (★ stars, last release) | Candidate 2 (★ stars, last release) |
|---|---|---|---|---|---|---|---|
| e.g. optimistic send | | | | | respond ≤100ms | | |
| e.g. delivered/read ticks | | | | | respond ≤100ms | | |
| e.g. typing indicator | | | | | respond ≤100ms | | |
| e.g. scroll stays put on new message | | | | | animate ≤16ms/frame | | |
| e.g. offline banner | | | | | load ≤1s | | |
| e.g. reconnect | | | | | load ≤1s | | |

Column notes:

- **Reference 1-3** -- rename each to the actual product (e.g. "Telegram"), one column per
  product named in step 1's research pass.
- **Ours today** -- what this repo does now for that affordance, or "nothing" if it doesn't
  exist yet.
- **RAIL budget** -- the number people feel: respond within 100ms, animate at 16ms/frame,
  load in under 1s (Google's RAIL model). Use whichever applies to that row.
- **Candidate N** -- an open-source library, UI kit, or named design pattern that already
  reproduces this affordance in one of the references, with its star count and last release
  date. Add or drop candidate columns as the research turns them up; add rows, not columns
  the steps above don't name.
