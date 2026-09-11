# Plan: fleet-kit-server-fleet

<!--
Schema: docs/plan/SCHEMA.md. Parsed by scripts/plan_rank.py (gru's build-eligibility
preference tier) and scripts/messenger_brief.py (Reif's morning brief) via the shared
resolver plan_rank.plan_path_for_instance(). Required: a heading reading exactly "Bets"
(any `#` level) whose non-blank lines each name zero or more issues via a `#<digits>`
token. Optional but present here: a `Checkpoints on the number:` line, read verbatim by
messenger_brief.vision().
-->

Gap to target: this instance is fleet-kit itself, not a venture -- it has no
`FLEET_NUMBER_URL` configured (`scripts/number_read.py` confirms: no number, no MRR gap to
report). Its job is keeping the fleet healthy enough to ship the ventures' numbers at all, so
the bets below are this board's own highest-priority open work (fleet:priority-high,
fleet:prd) rather than a dollar figure. gh#570's own PRD (marie, Part C0, 2026-09-11) sets this
as the intended seed path when a human hasn't yet decided whether this instance's plan should
instead mirror a venture's: "A builder can satisfy every criterion above today by seeding this
instance's own bets from this board's open high-priority work, and #571 rewrites it against
reality next week."

Checkpoints on the number: no MRR number on this instance; the checkpoint that matters here is
getting `#571` (the weekly rewrite) live so this file stops being hand-maintained.

## Bets

1. Judge-judy stops rubber-stamping blocked PRs through to merge -- #806
2. Deploy host stops landing on a dev branch after a failed cutover -- #834
3. A blocked journey asks a human instead of silently waiting five days -- #857
4. Sentry's evidence survives long enough to be read, not deleted minutes after it's written -- #856
5. Authority file tracks class -> level so answering an ask twice actually stops a third -- #771

## Notes

Seed content only (gh#570's own non-goal 1) -- a human or `#571`'s weekly rewrite is expected
to replace this wholesale once the open question above (whose plan this instance's file
should describe) is answered. Bet order is priority-list order pulled from this board on
2026-09-11, not a ranked commitment beyond that.
