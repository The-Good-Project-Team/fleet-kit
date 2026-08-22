# gru + minions — orchestrator/worker split (PRD, 2026-08-21)

Reif, watching the live runs feed: "the minions - not gru - or should they be just a single
session that has a bunch of sub-sessions." Decision, from the follow-up: **gru stays, and it
spawns a new member type called minions, whose job is to build the thing.** This doc specs
that split before touching code.

## Today (what exists, verified live on dino)

One member, `gru`, IS the worker. `run_gru_fanout.sh` (a bash script, not gru itself) reads
headroom, computes N via a Fibonacci ladder, then spawns N fully independent `run_member.sh
gru` processes — each its own `claude -p` session: claims one backlog item, builds in its own
worktree, opens a PR, arms auto-merge, exits. No shared state between them except the GitHub
Issues board (claim label) as the coordination point. Zero orchestration — literally N
unrelated bash-spawned processes racing each other to claim different items.

## The real constraint this design has to respect

**A headless `claude -p` process cannot spawn in-process subagents.** The `Agent`/subagent
tool this conversation uses is a Claude Code interactive-harness feature — it doesn't exist
for a bare CLI call. So "gru spawns minions" cannot mean "gru's own context tree fans out
sub-sessions the way this session does." The only real primitive available to gru is the same
one `run_gru_fanout.sh` already uses: **spawn more `claude -p` processes** (via `Bash` +
`Popen`/backgrounding, which gru already has in its tool allowlist).

The actual design question is narrower than "in-process vs subprocess" — it's:

**bash script dispatches N workers (today)** vs **gru's own Claude session dispatches N
minion processes and coordinates before finishing (proposed)**.

## Proposed shape

1. **`run_gru_fanout.sh` no longer computes N and spawns gru N times.** It spawns exactly
   ONE `run_member.sh gru` per tick. Gru becomes the orchestrator, not one of N identical
   racers.
2. **Gru's job, one pass (Reif, 2026-08-21 — this is the actual decision loop, spelled out
   because it's the whole point of gru existing as a session rather than a bash script):**
   - **Runway.** Read real headroom (maxx_reader.py's live pacing signal) and this box's own
     observed cost-per-build (fleet_db.py spend history) — how much work can genuinely be
     afforded this pass, in dollars, not a fixed guess.
   - **Priority — gru CHOOSES, marie RANKS (Reif's correction, 2026-08-21).** gru does not
     rank the backlog itself. Marie (the backlog PM, `members/marie/marie.md` Part C) scores
     every open item RICE-style against the target repo's own vision and writes it as a
     `fleet:priority-high/medium/low` label. gru reads that label, filtering high first, then
     medium, then low, and picks what fits its runway. An unlabeled item is lowest priority
     by default, not something gru corrects on its own judgment.
   - **Size N to fit BOTH constraints** — the runway ceiling AND how many genuinely
     high-priority items actually exist this pass (never pad N with low-value items just to
     spend the full budget — an empty or thin high-value queue means a small N, not "spend
     it all anyway").
   - **Claim.** Claim the chosen N items itself, serially, in its own turns (cheap, no
     subprocess needed) — this also removes the collision class entirely (today's design
     tolerates two workers racing the same item via git-merge-and-retry; gru claiming
     up front in one context never lets that race start).
   - **Spawn.** Launch N `run_member.sh minion` processes in the background (`Bash` +
     backgrounding), each in its OWN fresh worktree (unchanged from today's per-worker
     isolation — cheap, proven, no reason to share one tree across minions), each handed its
     specific pre-claimed item number explicitly. Minions never touch the claim step or the
     priority call — that authority stays with gru.
   - **Require reports.** Wait for every minion to actually wind down (poll, don't block past
     gru's own timeout budget) and READ each one's real result before gru's own pass is
     allowed to finish — a minion that's still running when gru would otherwise report is not
     a report gru gets to skip.
   - **Synthesize.** Write ONE combined outcome/self_critique — this pass's runway, the
     priority call gru made and why, and each minion's real result (PR opened / failed /
     found already-fixed) — to gru's own run record. Fixes the "5 identical rows" dashboard
     noise Reif flagged: one gru pass = one legible row with the real breakdown inside it,
     not N indistinguishable `reported_nothing` blocks.
3. **`minion` is a new member** (`members/minion/minion.fleet.json` + `minion.md`), a near-
   copy of gru's ORIGINAL charter (build/test/PR/auto-merge rules 1-10 are unchanged — they're
   already worker-shaped) MINUS the claim step (gru already claimed the item for it) and minus
   the runway/priority awareness (that's gru's job now, not the worker's). minion's own
   `gh issue edit` is tool-denied entirely — it has no legitimate reason to touch a label.
4. **Cost/efficiency note**: this does NOT reduce total `claude -p` process count for a full
   pass (still 1 gru + N minions = N+1 processes, vs today's N gru processes) — gru's own
   pass adds one session's worth of overhead on top. The efficiency win is elsewhere: no
   wasted claim-collisions (today's design pays for git-merge-conflict recovery on every
   collision; this design has none), and one legible run record per pass instead of N
   indistinguishable ones.

## Outcomes / acceptance

- A single gru pass claims 1-N items with no two minions ever assigned the same item (proof:
  a test asserting gru's own claim step is atomic/serial before any minion spawns).
- Dashboard shows ONE row per gru pass with a real per-minion breakdown (PRs opened, items
  that failed, items where the minion found the item already fixed), not N generic rows.
- `run_gru_fanout.sh` shrinks to "spawn one gru" — the Fibonacci-ladder headroom math moves
  INTO gru's own charter/prompt (it needs to reason about it to decide N, not have N handed
  to it by a bash script it can't see the reasoning of).
- Minion never claims from the board directly — verified by minion.fleet.json's tool
  allowlist NOT including whatever board-claim command gru uses (a minion that could
  self-claim could still race another minion; removing the capability removes the risk, not
  just discourages it).

## Resolved (Reif, 2026-08-21)

- **Own worktree per minion.** Confirmed — same collision-avoidance reasoning gru's charter
  already documents for concurrent workers. No shared tree.
- **Gru's job is explicitly: runway -> choose (from marie's ranking) -> claim -> spawn ->
  require reports.** Not "pick N unclaimed items and go" — a real choice, bounded by runway,
  of WHICH already-ranked items to build now — and gru does not get to finish its own pass
  without reading back a real result from every minion it spawned.
- **Marie ranks, gru chooses, minion builds.** Marie's charter gained a Part C: RICE-shaped
  priority scoring against the target repo's own vision, written as `fleet:priority-*`
  labels. gru never re-ranks; an item marie hasn't labeled is lowest priority by default.
- **Gru's own budget/timeout is bigger than a single minion's** — `max_budget_usd: 8`,
  `timeout_s: 3600` (vs minion's 5/2400) since it's now doing runway-read + choosing +
  claim-coordination + waiting on N children, all in one invocation.

## Built (2026-08-21)

- `members/minion/{minion.fleet.json,minion.md}` — new worker member.
- `members/gru/{gru.fleet.json,gru.md}` — rewritten as pure orchestrator (Edit/Write removed
  from its tool allowlist — if gru wants to touch a file, that's a bug in this design).
- `members/marie/{marie.fleet.json,marie.md}` — Part C added (priority ranking), turn/timeout/
  budget bumped (120/3600/$10) for the added scope of ranking the full backlog every pass.
- `scripts/run_member.sh` — `--item <n>` flag, prepended to the charter prompt so a minion
  knows its assigned issue even though every concurrently-spawned minion shares one charter
  file. `RUN_ID` includes the item number so gru's own report-reading grep can find it.
- `scripts/run_gru_fanout.sh` — collapsed to a one-line `exec run_member.sh gru`; all N-sizing
  logic moved into gru's own reasoning per its charter.
- `scripts/fanout.py` — DELETED (Reif, 2026-08-21: "dead code we kill, if writing is cheap
  then deleting has to be a law"). Its Fibonacci-ladder logic is superseded by gru's own
  in-context runway reasoning; nothing else called it. `fleet.env.example`'s
  `FLEET_HEADROOM_FRACTION`/`FLEET_GRU_HEADROOM_USD` removed alongside it (both dead the
  moment nothing read them).
