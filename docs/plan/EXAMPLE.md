# Plan (example)

This file is a worked example of `docs/plan/SCHEMA.md`'s format, checked in so
`scripts/test_plan_rank.py` parses a real file instead of a fixture string. It is not read by
any live instance -- no `$FLEET_INSTANCE_NAME` resolves to `EXAMPLE`.

Checkpoints on the number: $10.83 -> $1,000 MRR by the end of this quarter, then $25,000 by
2026-12-31.

## Bets

1. Verified Org badge live on every claimed nonprofit page -- #4494
2. Atlas search surfaces a funder's cause in front of noise -- #4495
- A bet naming two issues at once -- #10 #11
- A bet with no issue number is still valid, just not build-eligibility hint text

## Notes

Anything outside the `## Bets` heading (this section, the checkpoints line above) is prose for
a human reader; `plan_rank.py` never looks at it.
