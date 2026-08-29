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

No network. Specs validate, the report contract records silence as a status, overrides tune
dials and refuse authority, `fleet.env` is yours and untracked, schedulers exist for both
platforms, the account pool doesn't gate itself on its own log text, and the fleet-view write
routes are authenticated and fail closed. This caught a real break during the port —
`run_report.py` imported a scoring module that was never copied, so a fresh clone crashed on
import and nothing noticed.

Every check here is a bug that actually happened. Adding one when you fix something is the
cheapest way to stop it coming back.

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

## Deploying: where the box is, and why it silently stops updating

**The box is `dino`.** `ssh dino` — the SSH config lives in `~/Classified/dino/ssh/config` and
goes through the cloudflared tunnel (`ProxyCommand cloudflared access ssh --hostname
dino.luckymachines.co/ssh`), same hostname that serves the API on 8420. `lucky`, `lucky-vm` and
`lucky-host` are *different machines* and have no fleet-kit checkout — probing them is a dead
end, they are not this.

| thing | where |
|---|---|
| host checkout | `/home/ubuntu/fleet-kit` (**not** `/opt/fleet-kit`) |
| instance dir | `/home/ubuntu/fleet-kit/instances/nonprofit-atlas` |
| container | `philanthropy` (rootless podman, `podman ps`) |
| paths *inside* the container | `/fleet-kit/scripts/`, `/var/log/fleet-kit/` |
| public endpoint | `https://dino.luckymachines.co` → 8420 |

### Which fleet.env is live (there are two, only one is read)

There are **two** files named `fleet.env` on the box and they are not copies of each other:

| path | read by | role |
|---|---|---|
| `~/fleet-kit/instances/<instance>/fleet.env` | `deploy.sh:46`, bind-mounted to `/fleet-kit/fleet.env` (`deploy.sh:115`) | **live config — the only one that runs** |
| `~/fleet-kit/fleet.env` | nothing | leftover from a pre-instances single-fleet layout; a decoy |

`deploy.sh` requires `FLEET_INSTANCE_DIR` and sources only that directory's `fleet.env`. It
never reads the repo-root one. Both are gitignored, so `git status` will not warn you that you
edited the dead file.

**Always confirm against the container, never the host checkout:**

```bash
podman exec <container> grep '^FLEET_ACCOUNTS=' /fleet-kit/fleet.env
```

Confirmed live 2026-08-27: the root file read `FLEET_ACCOUNTS="primary"` while the running
fleet had `FLEET_ACCOUNTS="primary claude-reif"` and was failing over correctly. Anyone
diagnosing account failover from the root file would have concluded the pool had no failover
target and "fixed" a fleet that was already working.

### Which Claude account is the fleet actually spending?

`FLEET_ACCOUNTS` is a space-separated list tried **in order**; each name maps to
`$FLEET_CREDS_DIR/.claude-<name>` (default `$HOME`). `deploy.sh:94` mounts one credential dir
per name, so a name with no logged-in dir is a failover target that cannot actually
authenticate.

| account | email |
|---|---|
| `tgp` | reif@thegoodproject.net |
| `gmail` | reiftauati@gmail.com |

**Renaming an account is not a text edit.** The name is load-bearing in four places that must
change together — the credential dir, `FLEET_ACCOUNTS`, the exhaustion state file's key, and
the per-deploy bind mount — and every failure is silent. `deploy.sh:94` runs
`mkdir -p .claude-<acct>` for every name, so renaming in `fleet.env` without moving the
credential dir makes the next deploy create an **empty** one: the account comes up logged out
and only ever reports `unauthenticated`. Use the script, which moves all four and dry-runs by
default:

```bash
FLEET_INSTANCE_DIR=/home/ubuntu/fleet-kit/instances/nonprofit-atlas \
  bash scripts/rename_account.sh <old> <new>          # dry run
FLEET_INSTANCE_DIR=... bash scripts/rename_account.sh <old> <new> --apply
FLEET_INSTANCE_DIR=... bash scripts/deploy.sh          # required: remounts under the new name
```

An account that has hit its weekly limit is recorded in the pool's exhaustion state file with
its reset epoch, and every later tick **skips it without spending a call** until that time
passes — that is the `budget verdict=gated:exhausted_until_<epoch>` line, produced by
`_account_pool_budget_verdict()` (`account_pool.sh:100`) reading state this module wrote
itself. It is normal, healthy output, not an error.

```bash
podman exec <container> tail -20 /var/log/fleet-kit/account-pool.log
```

Reading `gated:` on the first account plus `call succeeded` on the next is failover **working**.
The fleet only stops when every account in the list is gated — the pool returns 3
(`ACCOUNT_POOL_ALL_EXHAUSTED`) and says `ALL accounts in '<list>' failed this call`.

Deploy is **blue-green** and is the only supported path — it builds a green candidate on alt
ports 8571/8572, health-checks it, cuts over, and keeps the previous build stopped as
`philanthropy-retired`:

```bash
ssh dino
cd ~/fleet-kit
FLEET_INSTANCE_DIR=/home/ubuntu/fleet-kit/instances/nonprofit-atlas bash scripts/deploy.sh
# roll back at any time:
bash scripts/deploy.sh --rollback
```

### A deploy waits for in-flight agent passes (2026-08-26)

Blue-green protects *serving*. It does nothing for *work*: agent passes run as children of the
blue container's cron, so the cutover's `podman stop` kills whatever is mid-pass. This ate a real
marie pass 17 issues into a complexity-scoring run — and, because the run record is written only
*after* `claude -p` returns, `runs.jsonl` got **no row at all**. ~$3 spent, and from every
dashboard the pass had simply never run.

So `deploy.sh` **drains before it builds**: while any pass is in flight it defers the whole
deploy, up to `FLEET_DRAIN_MAX_S` (default 1800s). `auto_deploy.sh` is a 5-minute poll, so a
deferred deploy just happens on a later tick. Past the bound it deploys anyway and says so —
a stuck pass must not block deploys forever.

Two consequences worth knowing:

- **A deploy can now take half an hour.** That is the gate working, not a hang. `auto_deploy.log`
  says `drain: N agent pass(es) in flight -- deferring cutover`. Watch the count fall.
- **`auto_deploy.sh` holds an `flock`** so those long holds cannot stack. Without it, every
  5-minute tick started *another* deploy (the state file is written only on success, so
  `LAST_DEPLOYED` stays stale while a deploy drains) — observed live as two concurrent deploys,
  the second heading for a cutover while the first still held.

A pass killed anyway — past the drain bound, or by a hand-run `podman stop` — is now recorded
with `status=killed`, distinct from `timed_out` (had time left) and `budget_declined` (was
spending fine). It means **interrupted and safe to re-run**. `SIGKILL` still cannot be trapped by
anyone, which is why the drain exists rather than relying on the trap alone.

### Why the box silently falls behind (confirmed live, 2026-08-26 — 19 commits behind)

`auto_deploy.sh` polls `main` and deploys when it moves, so nobody watches it. It **refuses to
act** in two states, by design, and both are silent unless you read `auto_deploy.log`:

1. **Dirty working tree.** It will not pull over uncommitted local changes (a hand-patched
   hotfix would be destroyed). Found live with two hand-patched scripts that had *already been
   merged upstream* — so the box blocked itself on edits it no longer needed.
2. **Checked out on a branch that isn't `main`.** Found live on a stale
   `feat/fleet-view-transcripts-kpi-evolution`. `git pull --ff-only origin main` then fails with
   `fatal: Not possible to fast-forward` — because the feature branch's commits were squash-merged
   upstream, so the content matches but the SHAs never will.

**Check both before assuming a merge shipped.** A merged, mutation-tested, "verified" PR is
still not running until this says `0`:

```bash
ssh dino 'cd ~/fleet-kit && git fetch -q origin && git branch --show-current && \
  git rev-list --count HEAD..origin/main && git status --porcelain'
# want: main / 0 / (no output at all)
```

`auto_deploy.sh` tests bare `git status --porcelain`, which **includes untracked files** — so a
stray `fleet.env.bak.*` left behind by an editor is enough to block every deploy tick
indefinitely, with the only trace a line in `auto_deploy.log`. Clean those up; don't leave them
sitting in the checkout.

Recovering a stuck box: confirm the local edits are already upstream
(`git diff --quiet origin/main -- <file>` → identical, safe to discard), back the diff up
anyway (`git diff > ~/fleet-kit-dirty-backup-$(date -u +%Y%m%d-%H%M%S).patch`), `git checkout --`
the files, `git checkout main`, pull, then run `deploy.sh`.

### Two gotchas that will waste your time

- **`selftest.py`'s `fleet.env.example present, fleet.env untracked` check FAILS on any real
  box, and that failure is expected.** It asserts `fleet.env` does not *exist*, but a live box
  must have one to run at all — the check conflates "untracked" with "absent". `12/13` with only
  that red is a healthy box. Verify the file is genuinely untracked
  (`git ls-files --error-unmatch fleet.env` → error = good) rather than "fixing" the tree.
- **Schema changes need a migration, not just a new column in `SCHEMA`.** `CREATE TABLE IF NOT
  EXISTS` is a no-op against the existing `fleet.db`, so a column added to `SCHEMA` alone reaches
  a fresh clone and *nowhere else*, while every insert on the live box fails on column count. Add
  it to `_ADD_COLUMNS` in `fleet_db.py` (expand-contract `ADD COLUMN`) so old rows read NULL and a
  rollback still works.

## Driving the fleet from outside the box

`fleet_view_server.py` serves a live dashboard and a small control API. Read routes (`GET`) are
open — it's a status page. **Every write route requires a shared secret**, sent as the
`X-Fleet-Key` header:

```bash
curl -X POST https://<your-host>/api/run_now \
  -H "X-Fleet-Key: $FLEET_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"member":"dumbledore"}'
```

Write routes: `run_now`, `fleet_toggle`, `steer`, `prune`, `close_pr`, `create_issue`,
`comment_issue`, `close_issue`.

### Read routes (GET, no key)

The server listens on `FLEET_VIEW_PORT`, **default 8420** — not the port you may have guessed
from the dashboard URL. All of these return JSON and send
`Access-Control-Allow-Origin: *`, so a browser on another origin can read them directly.

| route | size | what it carries |
|---|---|---|
| `/api/snapshot` | ~570KB | everything: `runs[]` (last 500) + `gh.{prs,issues,merged,self_evolution}` |
| `/api/query?member=&status=&item_id=&limit=100` | varies | filtered runs out of the sqlite mirror — prefer this over `snapshot` |
| `/api/stats/runs_summary?hours=24` | ~3KB | `signal_rate`, `executed`, `total`, `budget_wall`, `declined`, `dormant[]`, `statuses`, `agent_rates`, `hourly[]` |
| `/api/stats/token_usage?hours=24` | ~2KB | hourly buckets: input/output tokens, `cost_usd` |
| `/api/stats/backlog_history` | ~1.6KB | open-backlog trend (does its own `gh` calls; cached 120s) |
| `/api/spend` | ~1.3KB | per member: `runs`, `total_cost`, `avg_cost`, `total_turns`, `ok_runs` |
| `/api/kpi?hours=24` | ~1KB | per-member KPI rollup out of outcome prose |
| `/api/members` | ~37KB | full member specs (mandate, lane, schedule) |
| `/api/next_fires` | ~1.2KB | when each member fires next |
| `/api/fleet_state` | small | env flags: `FLEET_ENABLED`, `REPO_URL`, per-member on/off |
| `/api/pass_log?member=` | varies | tail of one member's log |
| `/api/stream` | SSE | live event stream |

**Shipped work** is `gh.merged[]` — each entry has `number`, `title`, `url`, `mergedAt`,
`headRefName`, `author`, and `files[]` with per-file `additions`/`deletions`.

```bash
curl -s https://<your-host>/api/stats/runs_summary?hours=24 | jq
```

Two things to know before pointing a page at these:

- **`/api/snapshot` is recomputed per request** and is by far the largest response. A page that
  polls it is a cheap way to load the box. Use `/api/query` or a `stats/*` route if you want a
  slice, and cache if you must poll.
- **They are open to anyone who knows the host.** A login in front of *your* page does not
  protect *this* data — the routes have no key. Treat the payload as public: it carries commit
  author names, agent `self_critique` / `prediction` / `last_verdict` text, and internal issue
  titles — the RSI fields are a member's unfiltered self-assessment, so read them as candid
  internal notes, not as copy anyone outside should see. If that is not acceptable,
  gate `do_GET` behind a read key and proxy the calls server-side (never from browser JS, where
  any key is public).

### Access log

Every API read appends one JSON line to `$FLEET_LOG_DIR/access.jsonl`: `ts`, `path`, `status`,
`ip`, `peer`, `cf_country`, `ua`, `origin`, `referer`.

Reads are anonymous by design, so **nothing here is asserted identity** — `ua`, `origin`, and
`referer` are supplied by the caller and are trivially forged. Behind a Cloudflare tunnel the
only field a caller cannot forge is `ip`, taken from `CF-Connecting-IP`; `peer` is the tunnel's
own loopback address on every remote request and is useless for attribution. This is for
characterising a traffic burst after the fact, not for authenticating anyone.

### Where the key lives, and how to get it

`FLEET_API_KEY` in the **instance's** `fleet.env` — `$FLEET_INSTANCE_DIR/fleet.env`, which on
dino is `/home/ubuntu/fleet-kit/instances/nonprofit-atlas/fleet.env`. **That file is the only
source of truth, and it is NOT the `fleet.env` at the repo root** — see "Which fleet.env is
live" below. Both are gitignored and never committed, so neither is in this repo and neither
can be recovered from git.
There is no secret manager in this kit by design (see extension points below); `fleet.env` is
where every other credential here lives too.

**Read the existing key** (do this before generating a new one — a fleet that has been deployed
already has one, and replacing it silently breaks every caller using the old value):

```bash
# on the box
grep '^FLEET_API_KEY=' /path/to/fleet-kit/fleet.env

# from your laptop, if the fleet runs in a container on a remote host
ssh <host> "grep '^FLEET_API_KEY=' /path/to/fleet-kit/fleet.env"
ssh <host> "podman exec <container> grep '^FLEET_API_KEY=' /fleet-kit/fleet.env"
```

**Set one for the first time** (or rotate — see below):

```bash
KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
printf '\nFLEET_API_KEY=%s\n' "$KEY" >> fleet.env
```

Write it to the **instance** `fleet.env` (`$FLEET_INSTANCE_DIR/fleet.env`). That path is
bind-mounted to `/fleet-kit/fleet.env`, so the container sees the edit with no rebuild — but
read the truncate-and-rewrite rule in `deploy.sh:194` first: a bind mount follows the *inode*,
so `sed -i` silently detaches the container's view. Restart the server so it picks the key up:
`fleet_view_server.py` reads it from its environment at request time, but only sees what it was
launched with.

**Rotating** is the same append, plus a server restart, plus updating every caller. Nothing
caches it, so a rotation takes effect on the next request. Rotate if the key ever appears in a
log, a PR comment, a CI transcript, or a chat window.

**Don't paste it into a conversation with an agent.** An agent that needs to trigger a run
should read it from `fleet.env` at call time and never echo it — the same rule this kit applies
to every other secret (`account_pool.sh`'s tokens, `FLEET_MAXX_KEY`, the webhook secret). A key
pasted into a transcript is a key in every context window that transcript touches.

**A tunnel is not authentication.** This is the section's whole reason for existing. This
server shipped with no auth and the advice "put it behind your own reverse proxy / tunnel if
you want it reachable off the fleet box" — which is exactly what happened, and a tunnel
*publishes* a service, it doesn't guard one. For a while anyone who knew the URL could POST a
member name and spawn `claude -p --dangerously-skip-permissions` on the box: someone else's
Anthropic budget, an agent with repo write access and a live `gh` token. Assume any host you
expose is being probed, and put the check in the app.

Two properties worth preserving if you touch this:

- **It fails closed.** With `FLEET_API_KEY` unset, remote writes are *refused*, not allowed. An
  unset key silently meaning "no authentication" is the exact state that caused the problem.
  Requests from localhost skip the check so an operator on the box is never locked out.
- **The key never reaches an LLM.** Every script that spawns `claude -p` sources `fleet.env`
  with `set -a`, which exports the key — so each one `unset`s it before the exec. Otherwise
  every member runs holding the ability to spawn unlimited runs, with the key sitting in nine
  agents' contexts where one prompt-injected issue body could print it into a PR comment. The
  thing the key protects must not be reachable by the things it protects against. `selftest.py`
  fails if a new spawner is added without the unset.

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

**Need a box first?** This whole install assumes a container-capable box already exists and
is reachable. If it doesn't yet, run `infra-kit/` first (its own `AGENT_INSTALL.md` if
you're an agent driving it) -- infra-kit stands up the VM/podman/tunnel, THIS install points
fleet-kit at it once it's up. Already have a box (existing server, another project's VM with
room for one more podman container)? Skip straight to step 1 below.

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
   (Linux) templates for: gitpull, rank, build, review, ceo, architect, account-health,
   tunnel-health. The last two are the fleet's only outage pagers (a fully-dead account pool,
   a 502'd public tunnel) — install them too, not just the build/review loop. Fill the
   `{{...}}` placeholders (repo path, label prefix, ntfy topic, public URL) and install per
   your platform's normal mechanism.
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
