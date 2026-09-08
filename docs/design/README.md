# Where a world-class research pass lives

`docs/quality-standard.md` §0 says what the research pass for a `quality:world-class` item
must produce, before any code. This is where it lands.

## The convention

For an item, put everything the research pass produces under `docs/design/<item>/`, where
`<item>` is the issue number (e.g. `docs/design/634/`):

```
docs/design/<item>/
  references/    -- screenshots or recordings, one subfolder per reference product
  parity-matrix.md   -- copy TEMPLATE-parity-matrix.md and fill it in
  spec.md             -- the design spec Reif approves
```

`docs/adr/<item>.md` (copy `docs/adr/TEMPLATE.md`) holds the buy-vs-build decision.

## The states to capture

Screenshot or record every state of the reference interaction and put them in
`docs/design/<item>/references/`, one subfolder per reference product
(quality-standard.md §0 step 2):

- empty
- first message
- sending
- sent
- delivered
- read
- typing
- offline
- reconnect
- error
- long thread
- phone width

Not every interaction has all twelve -- capture the ones that apply and say in `spec.md` which
you skipped and why.

## The two templates

- [`TEMPLATE-parity-matrix.md`](TEMPLATE-parity-matrix.md) -- the table: one row per
  affordance, one column per reference product, one for ours today, one for the RAIL budgets,
  and one per open-source library or named pattern that already reproduces the affordance.
- [`TEMPLATE.md`](../adr/TEMPLATE.md) in `docs/adr/` -- the buy-vs-build decision drawn from
  that matrix: which candidate was picked, or why hand-rolling won, in one named reason.

## What happens after the spec is written

The build does not start until Reif approves the spec. File that approval as an ask:

```
python3 scripts/ask.py file --member marie --why "approve design spec for #<item>" \
    --class decision --unblocks "build slices for #<item>"
```

Once Reif answers, post a comment on the issue reading `Design approved: <ask id>` --
`scripts/quality_gate.py` reads that line and only then lets build slices through.
