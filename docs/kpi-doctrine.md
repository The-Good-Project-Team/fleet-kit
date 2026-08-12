# KPI doctrine — for any lane/agent graded on a number

Provenance: genericized from nonprofit-atlas's `docs/ops/lane-kpis.md` (2026-08-04), written
after that fleet was found grading agents on ACTIVITY ("filed N items") rather than outcome —
which produced 200+ passes of one lane with a metric that never moved, and no single pass
caught it because nothing was checking. If you assign an agent (a scout, a lane owner, any
recurring pass) a number to move, these seven rules are what keep the number honest.

## The seven rules

**1. An agent never computes its own number.**
The KPI is computed by an independent job from primary data the agent does not write. An
agent that reports its own score is reporting a claim, not a measurement. Separate the
measurer from the measured — otherwise "I improved the metric" and "I changed how the metric
is computed" become indistinguishable, and the second one always wins on effort.

**2. Every KPI ships with a guardrail it may not degrade.**
They render together, always. **KPI improved + guardrail degraded over the same window =
BREACH — a failed pass, not a win.** Nearly every way to cheat a single metric is "move the
number, wreck something adjacent" (grow signups by making the funnel worse everywhere else;
grow throughput by shipping garbage that gets reverted). The guardrail is what makes that
visible instead of silently rewarding it.

**3. Denominators are pinned and watched.**
For any rate, store the denominator as its own metric point. If it moves more than ~10%
between consecutive readings, the KPI should render as VOID for that window, not as an
improvement. A rate that improved because its denominator shrank did not actually improve —
and it must be impossible to read that way from the number alone.

**4. Redefinition is an event, not a silent edit.**
Metric keys carry a version. Changing what a KPI *means* breaks the trend line visibly rather
than rebasing history underneath it. A label that stops matching its own query is a lie even
when the query is technically correct.

**5. Missing data reads STALE, never 0, never last-known.**
Older than 2x its expected interval → STALE (with a timestamp). Never computed → a dash, not
a zero. A not-yet-computed KPI rendered as 0 reads as catastrophe; a silently stale value
frozen at its last reading reads as calm. Both are worse than an honest gap — silence about a
broken measurement must itself be loud.

**6. Every pass states its KPI, the delta, and the item it filed to move it.**
"Nothing to report" is a claim, valid only in a line that carries the number, the delta, and
the specific reason no filed item would move it. Without those three things, the pass did not
actually examine anything — it just said so.

**7. Every filed item names the KPI it targets.**
No orphan work. An item that cannot name which KPI it's meant to move is either mis-scoped or
belongs to a different agent's lane.

## Adding a KPI

1. Register it with its guardrail, its denominator (if a rate), and its expected update
   interval. A KPI without a guardrail does not ship — see rule 2.
2. Name the CHEAT the guardrail blocks before you ship it. If you cannot name the cheat, you
   do not understand the metric yet, and neither will the agent grading against it.
