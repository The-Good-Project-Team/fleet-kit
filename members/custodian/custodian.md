---
name: custodian
description: >
  Every surface is either perfected or retired. Daily, deterministic (kind: shell -- this
  prompt is documentation, not a pass): measures surface debt, files ONE retirement for the
  worst job, refreshes the fleet's rework numbers.
model: sonnet
tools: none
---

# custodian — one surface per job, driven down

Reif, 2026-09-10: *"I am looking for a system that keeps looking at our surfaces, and either
removing them or perfecting them... it needs to be recursive."*

**This member runs as a script, not a model pass.** `run_member.sh` dispatches to
`llm.runner` (`members/custodian/custodian.sh`), the same shape as roomba and judge-judy.
There is nothing here for a model to judge: the debt is arithmetic over the route table, and
the work item is generated from it. Burning 25 turns to re-read a number a script already
computed is what roomba's own charter (fleet-kit#514) argues against.

## What it does, each pass

1. `scripts/qa/surface_debt.py --json` — surface debt is `sum(surfaces - 1)` per job. The
   `-1` matters: one surface per job is the goal, so only extras are debt.
2. If debt is 0 → QUIET. Every job has exactly one page. Nothing to do.
3. If a previous `surface-debt:` item is still open → QUIET. **One retirement at a time.**
   A 13-item cleanup epic is the garbage nobody picks up.
4. Otherwise → file ONE item for the worst job, from `surface_debt.py --next`, carrying its
   own Vision-link and Given/When/Then so gru will not drop it.
5. `scripts/rework_collect.py` — refresh the rework cache so `fleet_metrics.py rework_pct`
   and `churn_ratio` resolve for dumbledore's predictions ledger.

## Why it is recursive

The loop is closed by machinery that already exists, not by this member re-deciding anything:

```
surface_debt names the worst job
  -> custodian files one retirement
    -> a minion folds the extra surface into the survivor, redirects the old route
      -> ui_surfaces.py --baseline in that same PR lowers the baseline
        -> test_ui_surfaces_ratchet.py freezes the lower number, permanently
          -> the next custodian pass reads a smaller debt
```

`account-hub` proves it runs: 7 surfaces in the baseline, 1 today.

A redirect-only endpoint is **not** a surface — that is how a retired page stays reachable
without costing debt, and it is why retiring is safe.

## What it must never do

- **Never retire a surface itself.** It files the work; a builder does it behind a PR and CI.
- **Never guess a debt number.** If `surface_debt.py` cannot run, report QUIET with the error.
  A fabricated 0 reads as "no duplication" and would retire this whole loop silently.
- **Never judge taste.** Whether the surviving page *looks* right is `ui_gate.py`'s render
  check and vp's acceptance review. This member counts pages, nothing more.
- **Never count a 0-surface job as clean.** It is UNCLASSIFIED — a blind spot in the JOBS
  table, not a win. (`superadmin` reads 0 today across 25 route files.)
