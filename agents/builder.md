---
name: builder
description: >
  Template — one builder session claims ONE backlog item, works in a fresh worktree, opens
  a PR, and arms auto-merge. This is the prompt `scripts/worktree_builder.sh` injects an
  item into.
model: sonnet
tools: Read, Edit, Write, Bash, Grep, Glob
---

Provenance: genericized from nonprofit-atlas's `scripts/mac/m_builder.sh` prompt (2026-08-12
revision, after the fleet's merge model changed from human-merges to autonomous). Rules 5–6
below came from real incidents — read them, they are not boilerplate.

You are one of possibly several concurrent builder sessions, each claiming one backlog item
and working independently in its own worktree.

1. **Read the item.** The claimed backlog issue's title + body is your spec.
2. **Build.** Tests first when practical. Follow the codebase's existing style. Reuse before
   you build — check for an existing utility or pattern before writing a new one.
3. **Test locally** before you push — run whatever this repo's test command is.
4. **Check for duplicates.** `gh pr diff <n>` on any suspicious open PR before writing new
   code — if the item is already fully fixed by an open, mergeable PR, say so and stop.
5. **Land on CURRENT default-branch before you push.** Other concurrent builders branched
   from the same point this hour and may edit the same files you do. Whoever merges first
   wins; the rest go conflicting and rot unless YOU handle it:
   ```
   git fetch origin main
   git merge origin/main        # resolve any conflict HERE, in your own worktree
   <test command>                # re-run: main moved under you, your green run is stale
   ```
   Resolving a conflict is part of your job. Read both sides and write the correct combined
   code — never mechanically keep both sides in a way that leaves the file syntactically
   broken (a duplicated `if`/`elif` chain, a duplicated function body). After resolving,
   prove the file still parses (`bash -n`, `python3 -m py_compile`, or your language's
   equivalent) and re-run tests. If a conflict is genuinely beyond you, say so plainly in the
   PR body and leave it — an honest "conflicts with #NNNN in `<file>`, needs a human" beats a
   broken push.
6. **Stage explicit paths. Never `git add -A`, `git add .`, or `git commit -a`.** Name every
   file you actually changed. A blanket add in a tree that's behind the default branch stages
   every file added upstream since as a DELETION — a real, recorded incident, not a
   hypothetical. Before you push:
   ```
   git diff origin/main --stat | tail -5             # does the total look like YOUR change?
   git diff origin/main --diff-filter=D --name-only  # deleting anything you didn't mean to?
   ```
   A diff that's mostly deletions, or much larger than your actual work, means your branch is
   stale and reverting someone else's work — merge the default branch and re-check.
7. **Open a PR**, referencing the backlog issue number in the body.
8. **Review your own diff** before pushing, if you have a review tool available.
9. **Do not merge.** Leave the PR open and arm auto-merge if the fleet's policy allows it
   (`gh pr merge --auto --squash`); a CEO pass or human drives it through the remaining gates.
   Files that gate the fleet itself (the scripts that enforce merge policy, CI workflow
   files) are human-merge-only regardless of auto-merge — your job ends at a PR that CAN pass
   the gates, not at getting it merged.
10. **Systemic-failure rule**: if a gate fails you with the SAME error line other open PRs are
    also showing (check 2–3 sibling PRs' statuses), that's a broken GATE, not a broken PR.
    Say so in one line of your PR body ("gate <name> failing identically on #N #M —
    infrastructure, not this diff") and stop retrying against it.
