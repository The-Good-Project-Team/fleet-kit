<h1 align="center">
  <img src="docs/img/magikarp.gif" width="110" alt="a Magikarp, flopping"><br>
  fleet-kit <sub><sup>aka <b>magikarp</b></sup></sub>
</h1>

<p align="center">
  <i>The worst Pokémon in the game already contains a sea serpent.<br>
  Nothing gets added — it just has to survive long enough to become what it is.</i>
</p>

An unattended software-development fleet you point at a git repo. It runs the full loop —
observe, rank, build, review, gate, merge, deploy — with a human touching only the parts that
genuinely need judgment: initial auth, scheduler install, and a one-time branch-protection call.

## Why "magikarp"

Magikarp is the joke Pokémon. It is famously, aggressively useless — it flops on the ground, its
only move is Splash, and Splash does *nothing*. Not weak damage. Nothing. You catch one, you look
at it, you understand immediately that you have been given garbage.

And it evolves into Gyarados: a sea serpent that levels towns.

**Nothing is added.** No new part arrives, nothing is bolted on, you do not trade it for a better
creature. The entire capacity was inside the flopping fish the whole time — the only thing
separating the two is accumulated experience it could not skip. That is the idea, and it is why
the name fits an agent fleet better than anything else we could have called it.

A fleet arrives flopping. Every failure this kit guards against is one the source fleet actually
shipped: passes that burned tokens and produced nothing, jobs that reported healthy while doing
no work, an agent filing findings into a store nothing downstream read, six of seven lanes
obeying an instruction they had never been given. The failure mode is never a crash. **It is a
fleet that looks busy.** Someone had to go find out why, every time, and each guardrail here is
the residue of one of those investigations.

The part worth internalizing: none of those fixes made the fleet *more capable*. The loop was
always able to run unattended — the same scripts, the same model, the same repo. What was
missing was the accumulated experience that turns "it ran" into "it shipped": knowing that a
silent pass must be recorded as a status, that a turn cap must be measured before it is set,
that a definition living in two places will drift, that an unparseable config file fails quietly.
The kit is that experience, written down, so your fleet skips the levels ours had to grind.

**The name is also the metric.** In the source repo `magikarp` is a *score*: percent of the way
to a loop that runs unattended, measured by work that actually shipped with no human in the path
— never by whether a job fired. A fleet at 100% of its blockers with nothing shipped is still
flopping. Splash still does nothing.

<p align="center">
  <img src="docs/img/magikarp.gif" width="70" alt="Magikarp">
  &nbsp;&nbsp;→&nbsp;&nbsp;
  <img src="docs/img/gyarados.gif" width="150" alt="Gyarados">
</p>
<p align="center"><sub>same animal, nothing added</sub></p>

## The loop

```
GitHub Issues (board)  ->  rank (RICE)  ->  build (fresh worktree, claude -p)
    ^                                              |
    |                                              v
  deploy driver  <-  merge (auto-merge)  <-  review (claude -p) + CI gates
```

- **Board** = GitHub Issues, labeled `fleet:backlog` / `fleet:claimed`
  (`scripts/board_github.py`). Any repo with `gh` auth gets a working queue for free — no
  separate database, no dashboard to keep alive.
- **Rank** — a recurring pass reorders open backlog issues by RICE. See `agents/ceo.md` for the
  reasoning it applies.
- **Build** — `scripts/worktree_builder.sh` claims one item, works in a **fresh git worktree**
  (never the shared checkout — see `agents/persona_law.md`), opens a PR, arms `gh pr merge --auto`.
  Every pass — win or lose — appends one record to `$FLEET_LOG_DIR/runs.jsonl` via
  `run_report.py` (see "Receipts" below); `fleet_view_server.py` tails that file live.
- **Review** — `members/judge-judy/judge-judy.sh`: one `claude -p` pass per open PR with an
  untrusted-diff prompt, posting a `fleet-code-review` commit status, and its own `runs.jsonl`
  record either way.
- **Gate** — your CI plus that review status, wired as GitHub required status checks.
- **Merge** — GitHub's own auto-merge. No custom drain loop and no TOCTOU handling to write;
  GitHub already serializes it.
- **Deploy** — a 3-function driver contract (`scripts/deploy_driver.md`): `current_sha`,
  `deploy`, `health`. Bring your own — how you deploy is the most product-specific thing here.
- **jefe** (`members/jefe/jefe.md`) — a recurring deep session that keeps the fleet's own
  guardrails intact and unblocks stalled work, on a priority ladder (self → tools → policy →
  prod → backlog) so it never polishes features while its own tooling is broken. Never gates a
  merge — that's judge-judy's job, via auto-merge on green checks.
- **dumbledore** (`members/dumbledore/dumbledore.md`) — a once-daily opus pass with two jobs:
  find what's ROTTING and fix it at the causal layer (persona/flag/gate/prompt, not the
  symptom), and — the architect's job, folded in — decompose the product vision into ONE epic
  at a time when the board has room: a PRD plus PR-sized, builder-executable issues. Without
  this layer a fleet only ever produces increments.

## The four things that make it survivable

Extracted from a production fleet after 21 days and ~1,000 merged PRs. These are the parts
that were learned the expensive way, and they are what separates this from a cron job that
calls an LLM.

**1. One store.** A fleet member is one file: schedule, model, turn budget, tools, prompt. Not
a config row in one place and a scheduler entry in another — the source fleet ran two definition
stores for six days after a migration, only one of which had a reconciler, and the result was
that a prompt edit merged to `main` never reached the running agent. Six of seven lanes silently
followed an instruction they had never been given.

**2. Reporting the member cannot skip.** Lifecycle and token cost are written by the *wrapper*,
around the agent — never self-reported. Only the outcome is the agent's own words, and its
**absence is recorded as a status**, not as silence. A pass that ran and produced nothing must
not look like a pass that never ran. That distinction is the single most useful thing in the kit.

**3. Receipts.** Every `claude -p` pass books `num_turns`, `stop_reason`, and weighted token
cost. The source fleet ran for weeks with `--output-format json` available and unused, so nobody
could say which agent was spending the budget. When it was finally measured, one hourly member
was ~14M tokens and ~$10 per pass — about $245/day, invisible until someone looked.

**4. A ceiling.** Every request re-reads the whole context, so an agent session that never ends
gets quadratically expensive. Cap it, and make a truncated pass say so out loud.

## Portability

`schedulers/` ships both **launchd** (macOS) and **systemd timers** (Linux) for every job.

A `.plist` is just macOS's cron: what to run, how often, what environment, where output goes.
Two properties matter — launchd will not start a second copy of a job while one is running
(which is a stronger concurrency guarantee than a database lock), and an edit does not take
effect until the job is booted out and re-bootstrapped, so something must reconcile installed
state against the repo. `systemd` timers give you the same two properties by different names.

Everything product-specific is a variable in `fleet.env` — repo path, label prefix, account
names, models. No host, no absolute path, and no company string is baked into a script.

## A fleet member, in one file

`members/*.fleet.json` is the whole definition. Change it, merge it, and the reconciler makes
it live — there is no second place to keep in sync.

```json
{
  "name": "example-scout",
  "kind": "llm",
  "schedule": { "interval_s": 3600 },
  "timeout_s": 1800,
  "llm": {
    "model": "sonnet",
    "max_turns": 40,
    "prompt_file": "agents/builder.md",
    "tools": { "allow": ["Read", "Grep", "Bash(gh pr list:*)"],
               "deny":  ["Bash(gh pr merge:*)", "Bash(git push --force:*)"] }
  },
  "report": { "vision_link": "required" }
}
```

A `mechanical` member drops the `llm` block and adds `"command"`. Same schedule, same lifecycle,
same report row — it just has no turn budget.

**JSON, not YAML, on purpose.** The runner parses this on the fleet host with the standard
library alone. The source fleet's host had no `yaml` module, and that class of assumed
dependency — an interpreter or import that exists on the laptop and not where it must run — was
a recurring way to ship a member that silently never started.

**Tools are per-member, and they are authority.** `scripts/overrides.py` can retune
`max_turns`, `model`, `enabled`, and `schedule` live, without a PR, because the delivery rail is
exactly what fails during an incident. It will **refuse** to touch `prompt` or `tools`: a turn
budget is a dial, "may this agent merge PRs" is not.

## Verify before you schedule

```
python3 scripts/selftest.py
```

Five checks, no network: specs validate, the report contract records silence as a status,
overrides tune dials and refuse authority, `fleet.env` is yours and untracked, and schedulers
exist for both platforms. This caught a real break during the port — `run_report.py` imported a
scoring module that was never copied, so a fresh clone crashed on import and nothing noticed.

## Edited fleet.env but the container didn't notice?

`fleet.env` is bind-mounted `:ro` into the container. Most edits (append a line, change a
value with an editor that truncates-and-rewrites) show up immediately — the container reads
the file fresh on every member invocation, no restart needed. But `sed -i` and some editors
replace-then-rename instead, which swaps the underlying inode; a mount that already resolved
the old one can keep serving stale content until the mount itself is torn down and recreated.
If a value you just edited doesn't show up inside the container (`podman exec <name> cat
/fleet-kit/fleet.env` still shows the old value), that's this — restart the container:

```
scripts/refresh_container.sh fleet-kit-<name>
```

Use this instead of a bare `podman restart` — on a box with rootless podman and published
ports, a plain restart can race its own port cleanup (`rootlessport listen tcp ...: address
already in use`) if the new instance binds before the kernel releases the old one's port. This
script retries via `podman start` automatically when that happens, instead of leaving the
container down.

## What's NOT in this kit (extension points)

The source fleet had product-specific machinery this kit deliberately does not port:
- **A heavy dashboard.** The source product's fleet HUD was ~2,700 lines across five files —
  its own Postgres tables, its own API layer, its own auth-gated routes riding on the app it
  existed to watch. This kit ships the opposite bet instead: `scripts/fleet_view_server.py`, one
  stdlib process (no framework, no DB) that tails `runs.jsonl` and polls `gh pr/issue list`,
  pushing both over SSE to one static page (`scripts/fleet_view.html`). Kill it and the
  build/review/merge loop is untouched — it's a window, not a component. See the `view` row in
  `schedulers/README.md`. It has no auth of its own; put it behind your own reverse proxy or
  tunnel for remote access (see `docs/deployment-learnings.md` #15 for a Cloudflare Tunnel
  recipe) rather than exposing the raw port.
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

An operator still needs a shell on the fleet box from off its LAN, though — see
`docs/deployment-learnings.md` #14-15 for the live-IP-resolution pattern (VMs move; never
hardcode one) and the Cloudflare Tunnel setup (mint an account-scoped Tunnel:Edit token, no
`cloudflared login` browser flow needed).

## Don't have a box yet? `infra-kit/`

Product-agnostic, doesn't need fleet-kit itself: a multipass VM, podman inside it, a
Cloudflare Tunnel reaching it from the internet on one hostname with path-scoped routes (no
subdomain sprawl), and the ssh config to reach it three ways (direct, LAN, tunnel). Three
scripts, run once each — see `infra-kit/README.md`. fleet-kit's own `up.sh` (below) is one
thing you can point at the box once it exists.

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
