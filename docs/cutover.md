# Cutover pattern — soak-gated rollout for any fleet-kit change

Provenance: genericized from nonprofit-atlas's `docs/ops/fleet-v2-cutover.md`, written when
that fleet moved from a custom merge-drain loop to GitHub's own auto-merge and from a
proprietary board to Issues. The pattern generalizes to any change that replaces a
load-bearing piece of the loop: write down what depends on it TODAY, what has to prove itself
BEFORE the old path is removed, and how to roll back at each step.

## Why stage instead of cut over in one PR

Two things are usually load-bearing simultaneously with any new piece you're introducing:
the OLD path is still the only thing keeping the loop running, and the NEW path has zero
hours of live evidence. Cutting both over in one commit means the first real failure of the
new path has no fallback — recreating exactly the "gate red-by-construction, whole queue
jams" failure class, but at whichever layer you just replaced.

## The pattern

For any load-bearing swap (merge mechanism, board backend, deploy driver, review tool):

1. **Write the preconditions.** What must be true, measurably, before the new path takes
   over? Not "it works" — a specific, checkable signal with a command to run. Example:
   "the new review path posts a verdict for every open PR within 2 hours, for 7 consecutive
   days" is checkable; "review seems reliable" is not.
2. **Run both paths in parallel** (shadow mode) long enough to gather the precondition
   evidence. The old path stays authoritative; the new path's output is compared, not acted
   on, until preconditions are met.
3. **Cut over one piece at a time**, each with its own rollback:
   - What's the exact command/config change to revert this one piece?
   - Does reverting it leave the system in a WORKING state (not "broken differently")?
4. **Retire the old path only after the new path has carried real load** for a defined
   period with the preconditions holding — not the day the new code merges.

## Template

```markdown
# <change name> cutover

Status: PENDING SOAK | SOAKING (since <date>) | CUT OVER (<date>) | ROLLED BACK (<date>, why)

## Preconditions
| # | Precondition | How to verify |
|---|---|---|
| P1 | ... | exact command |

## Step 1 — <what changes>
<the change>
Rollback: <exact command/config revert>

## Step 2 — <next change, after Step 1's soak period>
...

## What does NOT change
<explicitly name what stays as-is, so a reader doesn't assume a wider blast radius than intended>
```
