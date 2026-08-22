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
2. **Gru's job, one pass:**
   - Read headroom (maxx_reader.py's real signal, same as today) + the board.
   - Decide how many minions it can afford this pass and pick that many DISTINCT claimable
     items up front, in its own context, avoiding two minions racing for the same item (the
     thing today's design tolerates via git-merge-and-retry — this removes the collision
     class entirely instead of recovering from it).
   - Claim all N items itself (one `gh issue edit --add-label fleet:claimed` per item,
     serially, in its own turns — cheap, no subprocess needed for this part).
   - Spawn N `run_member.sh minion` processes in the background (`Bash` tool, backgrounded),
     one per claimed item, passing the item number explicitly (minions do NOT re-claim from
     the board — they're handed a specific pre-claimed item, removing the claim race
     entirely).
   - Wait for all N to finish (poll, don't block synchronously past its own timeout budget).
   - Read back each minion's result (PR opened? failed? conflict?) and write ONE combined
     outcome/self_critique to its own run record — this is the fix for the "5 identical rows"
     dashboard noise Reif flagged: one gru pass = one row, with the real N-minion breakdown
     inside it, instead of N separate `reported_nothing` rows that read as noise.
3. **`minion` is a new member** (`members/minion/minion.fleet.json` + `minion.md`), a near-
   copy of gru's current charter (build/test/PR/auto-merge rules 1-10 are unchanged — they're
   already worker-shaped) MINUS the claim step (gru already claimed the item for it) and minus
   the fanout/headroom awareness (that's gru's job now, not the worker's).
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

## Open questions for Reif before building

- Does minion get its OWN worktree per item (yes, almost certainly — same collision-avoidance
  reasoning gru's charter already documents for concurrent workers), or does gru manage one
  shared worktree tree and dispatch minions into subdirectories of it? (Recommend: own
  worktree per minion, unchanged from today — cheap, already proven, no reason to share.)
- Does gru's own pass get a bigger budget/timeout ceiling than a single minion (it's now
  doing claim-coordination AND waiting on N children within one invocation)? Needs its own
  `max_budget_usd`/`timeout_s` tuned separately from minion's.
