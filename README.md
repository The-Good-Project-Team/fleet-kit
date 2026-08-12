# fleet-kit

An unattended software-development fleet you point at a git repo. It runs a full loop —
observe → rank → build → review → gate → merge → deploy — with a human touching only the
parts that genuinely need judgment: initial auth, scheduler install, and a one-time branch
protection call.

Extracted 2026-08-12 from 990 Scout's production fleet (`nonprofit-atlas`) after an audit
found the loop working end-to-end for 21 days (1,000 PRs merged) but structurally coupled to
one product's box, DB, and API. This kit is that fleet with every product-specific string
pulled into one env file.

## The loop

```
GitHub Issues (board)  →  rank (RICE)  →  build (fresh worktree, claude -p)
    ↑                                            │
    │                                            ▼
  deploy driver  ←  merge (auto-merge)  ←  review (claude -p) + CI gates
```

- **Board** = GitHub Issues, labeled `fleet:backlog` / `fleet:claimed` (`scripts/board_github.py`).
  Any repo with `gh` auth gets a working queue for free — no separate DB, no dashboard.
- **Rank** — a daily/hourly pass reads open backlog issues and reorders by RICE. Not shipped
  as a standalone script here (it's a thin `gh issue list` + `claude -p` loop); see
  `agents/ceo.md` for the reasoning it should apply.
- **Build** — `scripts/worktree_builder.sh` claims one backlog item, works in a FRESH git
  worktree (never the shared checkout — see `agents/persona_law.md`), opens a PR, arms
  `gh pr merge --auto`.
- **Review** — `scripts/code_review_local.sh`: one `claude -p` pass per open PR, untrusted-diff
  prompt, posts a `fleet-code-review` commit status.
- **Gate** — your CI (tests, lint, whatever your repo needs) plus the review status above, wired
  as GitHub required status checks via branch protection.
- **Merge** — GitHub's own auto-merge. No custom drain loop, no TOCTOU handling to write —
  GitHub already serializes this.
- **Deploy** — a 3-function driver contract (`scripts/deploy_driver.md`): `current_sha`,
  `deploy`, `health`. Bring your own script; the kit ships no deployer because "how you deploy"
  is the most product-specific thing in the whole loop.
- **CEO pass** — a daily/hourly deep session (`agents/ceo.md`) that keeps the fleet's own
  guardrails intact and unblocks stalled work, using a priority ladder (self → tools → policy →
  prod → backlog) so it never spends a pass polishing features while its own tooling is broken.
- **Architect pass** — a daily deep session (`agents/architect.md`) that decomposes your product
  vision into ONE feature epic at a time: a PRD plus a sequence of PR-sized, builder-executable
  issues. Without this layer, a fleet only ever produces increments — this is what turns
  increments into features.

## What's NOT in this kit (extension points)

The source fleet had product-specific machinery this kit deliberately does not port:
- **A dashboard.** The source product had a live ops HUD reading fleet telemetry off its own
  DB. This kit reports via plain log files + GitHub — wire your own dashboard reading
  `gh issue list` / `gh pr list` / the schedulers' logs if you want one.
- **A browser-VM design gate.** The source product used a hosted browser-agent service to
  render UI changes and judge them visually. If your product has a UI, add an equivalent gate
  as another required status check — the merge/gate architecture here doesn't care what
  produces the status, only that it's a GitHub check.
- **A token-spend account pool across many concurrent consumers.** `scripts/account_pool.sh` is
  included and genericized (space-separated `FLEET_ACCOUNTS`), but if you only ever run one
  Claude account, you can skip it and call `claude -p` directly in each script.
- **Deterministic KPI-delta scouting.** `docs/kpi-doctrine.md` documents the rules (never
  self-graded, guardrail-paired, STALE-never-0) for when you build lane-specific health
  scouts; no scout scripts ship here since "what's a lane" is entirely product-specific.

## Install (target: under 15 minutes to first unattended PR)

1. **Copy this kit** into the target repo as `.fleet/` (or clone it as a sibling and point
   `FLEET_REPO` at the target — either layout works, nothing here assumes a specific path).
2. **Fill `fleet.env`** — copy `fleet.env.example`, set `FLEET_REPO`, `FLEET_LABEL_PREFIX`,
   `FLEET_ACCOUNTS`. Leave everything else at its default for a first run.
3. **Auth** — `gh auth login` (once, on the box that runs the fleet) and `claude` auth for
   every account named in `FLEET_ACCOUNTS`. This is the one step that must be a human: no
   script in this kit can complete OAuth for you.
4. **Seed the board** — `gh label create fleet:backlog` (the scripts also do this idempotently
   on first write) and file one issue by hand to prove the plumbing: `gh issue create --title
   "test: fleet wiring" --label fleet:backlog`.
5. **Run one pass by hand** before scheduling anything: `bash scripts/worktree_builder.sh` —
   confirm it claims the seed issue, opens a worktree, and (even if it can't finish the task)
   exits cleanly with a report. This is the fastest way to catch a misconfigured `fleet.env`.
6. **Install the scheduler.** `schedulers/` has both launchd (macOS) and systemd timer
   (Linux) templates for: gitpull, rank, build, review, ceo, architect. Fill the `{{...}}`
   placeholders (repo path, label prefix) and install per your platform's normal mechanism.
7. **Branch protection** (one-time, human call — this changes repo settings):
   ```
   gh api -X PUT repos/{owner}/{repo}/branches/main/protection \
     -f required_status_checks.strict=false \
     -f 'required_status_checks.checks[][context]=fleet-code-review' \
     -f enforce_admins=false -f required_pull_request_reviews=null -f restrictions=null
   ```
   Add your own CI check contexts to the same call. Builders arm `gh pr merge --auto` per-PR;
   GitHub merges once every required check is green.

## Success metric

**Time from step 1 to the first PR GitHub auto-merges with zero human in the loop.** The
source fleet's own audit used this number to judge whether the architecture itself — not any
one script — was worth the complexity. Track it the same way here.

## Provenance

Every file below carries a comment naming the source-fleet file it was extracted from, for
anyone who wants the fuller battle-tested version (dashboard integration, Cursor VM gate,
Postgres-specific CI lanes, etc.) as a reference.
