#!/bin/bash
# worktree_builder.sh — claim ONE backlog item, build it in a fresh worktree, open a PR.
#
# Provenance: distilled from nonprofit-atlas's `scripts/mac/m_builder.sh` +
# `scripts/mac/m_builder_worktree.sh` (the worktree-creation lock pattern below is kept
# close to verbatim — it fixes two real, measured races: concurrent builders colliding on
# `git worktree add` in the same second, and a killed builder leaving a stale local branch
# that wedges every future attempt at the same item).
#
# Run this once per builder slot per tick (a scheduler can spawn several in parallel — the
# lock below serializes only the worktree-creation moment, not the whole build).
#
# Env (fleet.env): FLEET_REPO, FLEET_LOG_DIR, FLEET_BUILDER_MODEL (default sonnet),
# FLEET_BUILDER_MAX_TURNS (default 60), FLEET_BUILDER_TIMEOUT seconds (default 2400).
set -uo pipefail

# set -a/+a: a plain . only sets local shell vars, invisible to claude -p (a separate
# exec) -- see run_member.sh for the full incident writeup (2026-08-22 fleet-wide auth outage).
[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-./fleet.env}"; set +a; }
# Never hand the fleet-view write key to an LLM pass -- it authorizes POST /api/run_now,
# which spawns agent runs on this box. See run_member.sh's fuller note. Least privilege.
unset FLEET_API_KEY


REPO="${FLEET_REPO:?set FLEET_REPO in fleet.env}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
LOG="$LOG_DIR/builder.log"
MODEL="${FLEET_BUILDER_MODEL:-sonnet}"
MAX_TURNS="${FLEET_BUILDER_MAX_TURNS:-60}"
TIMEOUT_S="${FLEET_BUILDER_TIMEOUT:-2400}"
WORKER_NAME="${FLEET_WORKER_NAME:-builder-$$}"
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# Master kill switch, then this member's own switch -- both checked BEFORE anything that costs
# money or touches the board (no worktree, no claim, nothing). Two flags, same file
# (fleet.env), same mechanism: FLEET_ENABLED for the whole fleet, FLEET_BUILDER_ENABLED for
# just this member -- so "turn everything off" and "turn off just the builder" are the same
# kind of edit, not two different systems.
. "$KIT_DIR/scripts/fleet_enabled.sh"
fleet_enabled_or_exit "builder"
if [ "${FLEET_RUN_NOW:-0}" != "1" ] && [ "${FLEET_BUILDER_ENABLED:-false}" != "true" ]; then
  log "builder: FLEET_BUILDER_ENABLED != true -- exiting without doing anything"
  exit 0
fi

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }
[ -f "$KIT_DIR/scripts/account_pool.sh" ] && . "$KIT_DIR/scripts/account_pool.sh"
command -v account_pool_run >/dev/null 2>&1 || account_pool_run() { "$@"; }

# gh#183: same guard as run_member.sh -- see its own comment for the full incident writeup.
# A stale vendored /fleet-kit copy can be missing this file even though `main` already has it;
# under `set -uo pipefail` (no -e) a plain `.` on a missing file used to no-op silently, leaving
# check_repo_clean_postflight undefined and the worktree-leak safety net (#78) silently off.
if ! { . "$KIT_DIR/scripts/postflight_dirty_check.sh"; } 2>>"$LOG" || ! command -v check_repo_clean_postflight >/dev/null 2>&1; then
  log "CRITICAL: postflight_dirty_check.sh failed to source from $KIT_DIR/scripts/postflight_dirty_check.sh -- worktree-leak safety net is DISABLED for this pass (stale vendored /fleet-kit copy? see gh#183/#140)"
  check_repo_clean_postflight() {
    log "CRITICAL: check_repo_clean_postflight called but the real guard never loaded -- worktree-leak check SKIPPED (run ${1:-unknown})"
  }
fi

# --- STEP 1: claim one item -------------------------------------------------------------------
CLAIM_JSON=$(python3 "$KIT_DIR/scripts/board_github.py" claim "$WORKER_NAME" 1 2>>"$LOG")
ITEM_ID=$(echo "$CLAIM_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["id"] if a else "")' 2>/dev/null)
if [ -z "$ITEM_ID" ]; then
  log "no unclaimed backlog items -- nothing to build this tick"
  exit 0
fi
ITEM_TEXT=$(echo "$CLAIM_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["text"])' 2>/dev/null)
ITEM_CONTEXT=$(echo "$CLAIM_JSON" | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["context"])' 2>/dev/null)
log "claimed item #$ITEM_ID: $ITEM_TEXT"

# --- STEP 2: fresh worktree, with the collision + stale-branch lock ----------------------------
WT_PATH="${TMPDIR:-/tmp}/fleet-build-${ITEM_ID}-$$"
WT_BRANCH="build/${ITEM_ID}-${WORKER_NAME}"
LOCK="${TMPDIR:-/tmp}/fleet-kit-worktree-add.lock"

create_build_worktree() {
  local attempt rc=1 waited held
  for attempt in 1 2 3; do
    waited=0; held=0
    while [ "$waited" -lt 120 ]; do
      if mkdir "$LOCK" 2>/dev/null; then held=1; break; fi
      # Steal a lock older than 5 min: a builder killed mid-add would otherwise wedge every
      # later attempt forever.
      if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +5 2>/dev/null)" ]; then
        rmdir "$LOCK" 2>/dev/null || true; continue
      fi
      sleep 2; waited=$((waited + 2))
    done
    if [ "$held" -ne 1 ]; then
      log "create_build_worktree: could not acquire lock within 120s (attempt $attempt)"
      sleep $((attempt * 3)); continue
    fi
    git worktree prune >/dev/null 2>&1
    # Delete-and-recreate, never reuse: a stale branch from a prior dead attempt would
    # otherwise silently build on top of possibly-broken prior commits.
    if git rev-parse --verify --quiet "refs/heads/$WT_BRANCH" >/dev/null 2>&1; then
      git branch -D "$WT_BRANCH" >/dev/null 2>&1
    fi
    git worktree add "$WT_PATH" -b "$WT_BRANCH" origin/main
    rc=$?
    rmdir "$LOCK" 2>/dev/null || true   # safe: reached only when held=1
    [ "$rc" -eq 0 ] && return 0
    log "create_build_worktree: attempt $attempt failed (rc=$rc), retrying"
    sleep $((attempt * 3))
  done
  return "$rc"
}

if ! create_build_worktree; then
  log "FATAL: could not create worktree for item #$ITEM_ID"
  exit 1
fi
# BUILD_SUCCEEDED flips to 1 only once a PR genuinely opens (STEP 4, below). Any exit before
# that -- RC != 0, timeout, kill signal, an uncaught error in this script itself -- releases
# the claim so the item goes back in the pool instead of sitting fleet:claimed forever with no
# worker on it. This is the gap deployment-learnings.md #7 names as "not yet fixed": found for
# real blocking dino's own first deploy (nonprofit-atlas issue #3044 -- 20/20 open backlog
# items already stuck claimed from an earlier run that had no release path).
BUILD_SUCCEEDED=0
cleanup() {
  # Check BEFORE removing the worktree -- see postflight_dirty_check.sh (fleet-kit#78).
  # RUN_ID isn't assigned until STEP 4 (after the build call) -- if this trap fires earlier
  # (killed mid-build), fall back to WORKER_NAME, not just $ITEM_ID: WORKER_NAME already
  # carries $$/a per-worker identity (see its default above), so the fallback stays as
  # disambiguated as the real RUN_ID would have been, instead of collapsing to one label
  # per item regardless of which concurrent attempt was actually running.
  check_repo_clean_postflight "${RUN_ID:-build-$ITEM_ID-$WORKER_NAME}"
  git -C "$REPO" worktree remove --force "$WT_PATH" >/dev/null 2>&1 || true
  git -C "$REPO" worktree prune >/dev/null 2>&1 || true
  rm -f "${USAGE_FILE:-}"
  if [ "$BUILD_SUCCEEDED" -ne 1 ]; then
    python3 "$KIT_DIR/scripts/board_github.py" release "$ITEM_ID" \
      "released by worktree_builder.sh: build did not produce a PR (see $LOG)" >>"$LOG" 2>&1
  fi
}
trap cleanup EXIT

# --- STEP 3: build ------------------------------------------------------------------------------
CHARTER=""
[ -f "$KIT_DIR/agents/builder.md" ] && CHARTER=$(awk 'BEGIN{d=0} /^---$/{d++; next} d>=2{print}' "$KIT_DIR/agents/builder.md")
PROMPT="$CHARTER

## This item (injected by worktree_builder.sh)
ID: $ITEM_ID
Title: $ITEM_TEXT
Context: $ITEM_CONTEXT

You are in a fresh worktree at $WT_PATH on branch $WT_BRANCH. Commit + push from here.

## Report (injected — end your final message with exactly these two lines)
Outcome: <one line — what you did, naming the PR # if you opened one, or why you stopped>
Evidence: <the PR URL / commit sha / file:line that proves it, or the exact error you hit>"

log "building item #$ITEM_ID in $WT_PATH (model=$MODEL)"
# --dangerously-skip-permissions, not --permission-mode acceptEdits: acceptEdits only
# auto-approves file Write/Edit, it still gates Bash execution behind an interactive
# approval prompt. An unattended builder in an isolated fresh worktree has no human to
# answer that prompt -- it hard-blocks forever (or, if the model is well-behaved, gives up
# and reports the wall instead of faking a result, which is what surfaced this: a real run
# against nonprofit-atlas wrote a correct fix + test, then couldn't run `bash -n`, `git add`,
# or `pytest` at all, and correctly refused to fabricate a passing result). The worktree
# IS the isolation boundary already (fresh clone, throwaway branch) -- that's what makes
# skipping the prompt safe here specifically.
#
# --setting-sources user: the TARGET repo's own CLAUDE.md/.claude/settings.json/hooks are
# for that repo's own interactive sessions -- a repo that runs its own persona/orchestrator
# system (e.g. "no explicit assignment -> you ARE the orchestrator, which is hook-blocked
# from writing code") will silently hijack an unattended `claude -p` call into that identity
# instead of following this kit's injected CHARTER prompt. Measured live: with project
# settings loaded, every build against such a repo returned a greeting as the target repo's
# own orchestrator persona and never touched a file, with zero error -- worktree_builder.sh
# looked like it was doing nothing across three full attempts before this was found.
# `user` scope keeps auth/model preferences, drops project-level CLAUDE.md/hooks/settings,
# so builder.md's own charter (injected below) is what actually governs the session.
# --output-format json: the provider's own per-call accounting (cost/tokens/turns), not a
# hand-rolled estimate -- see pass_accounting.py's header. --max-budget-usd is a CLI-enforced
# hard backstop per call, independent of anything this kit measures after the fact.
# `RAW=$(...)` is a command substitution, i.e. a SUBSHELL -- account_pool.sh's
# `export ACCOUNT_POOL_SELECTED` cannot escape it, so the failure log below recorded
# `account=none` for every failed build. Hand the pool a file and read it back.
ACCOUNT_POOL_SELECTED_FILE=$(mktemp "${TMPDIR:-/tmp}/fleet_acct.XXXXXX")
export ACCOUNT_POOL_SELECTED_FILE
RAW=$(cd "$WT_PATH" && account_pool_run timeout "$TIMEOUT_S" claude -p "$PROMPT" \
  --model "$MODEL" --dangerously-skip-permissions --setting-sources user \
  --max-turns "$MAX_TURNS" --output-format json \
  --max-budget-usd "${FLEET_MAX_BUDGET_USD:-5}" 2>>"$LOG")
RC=$?
[ -s "$ACCOUNT_POOL_SELECTED_FILE" ] && ACCOUNT_POOL_SELECTED=$(cat "$ACCOUNT_POOL_SELECTED_FILE")
rm -f "$ACCOUNT_POOL_SELECTED_FILE"
OUT=$(printf '%s' "$RAW" | python3 "$KIT_DIR/scripts/pass_accounting.py" text)
USAGE_FILE=$(mktemp "${TMPDIR:-/tmp}/fleet_usage.XXXXXX")
printf '%s' "$RAW" | python3 "$KIT_DIR/scripts/pass_accounting.py" usage > "$USAGE_FILE" 2>/dev/null

if [ "$RC" -ne 0 ]; then
  log "item #$ITEM_ID: build session failed rc=$RC (account=${ACCOUNT_POOL_SELECTED:-none})"
  RUN_ID="build-${ITEM_ID}-$$-$(date +%s)"
  echo "$OUT" | python3 "$KIT_DIR/scripts/run_report.py" \
    --member "builder" --run-id "$RUN_ID" --kind llm --exit-code "$RC" --pass-file - \
    --item-id "$ITEM_ID" --usage-file "$USAGE_FILE" >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"
  exit 1
fi

# --- STEP 4: stamp the backlog id onto the PR (idempotent) --------------------------------------
# Same lesson as the source fleet: a prose "mention the item ID" instruction has a real-world
# compliance rate well under 100%. Stamp it deterministically here rather than hoping.
PR_NUM=$(grep -oE 'github\.com/[^ ]+/pull/[0-9]+' <<<"$OUT" | tail -1 | grep -oE '[0-9]+$')

# --- report: one line in runs.jsonl per pass ------------------------------------------------
# This is the leg the fleet-view server tails. Written here (by the wrapper, around the agent)
# rather than left to the agent to self-report -- see run_report.py's own header for why that
# split is load-bearing, not stylistic. Written AFTER PR_NUM resolves so the record carries it
# deterministically instead of relying only on the agent's own Evidence: line.
RUN_ID="build-${ITEM_ID}-$$-$(date +%s)"
echo "$OUT" | python3 "$KIT_DIR/scripts/run_report.py" \
  --member "builder" --run-id "$RUN_ID" --kind llm --exit-code "$RC" --pass-file - \
  --item-id "$ITEM_ID" --usage-file "$USAGE_FILE" ${PR_NUM:+--pr "$PR_NUM"} >> "$LOG_DIR/runs.jsonl" 2>>"$LOG"

if [ -n "$PR_NUM" ]; then
  BUILD_SUCCEEDED=1   # a real PR exists; the claim now belongs to that PR's review/merge cycle,
                       # not to this build pass -- cleanup() must not release it back to the pool.
  BODY=$(gh pr view "$PR_NUM" --json body -q '.body' 2>/dev/null || echo "")
  if ! grep -qE 'Backlog:[[:space:]]*#[0-9]+' <<<"$BODY"; then
    printf '%s\n\nBacklog: #%s\n' "$BODY" "$ITEM_ID" | gh pr edit "$PR_NUM" --body-file - >/dev/null 2>&1
  fi
  # NO STRATEGY FLAG. `main` is merge-queue-controlled, and an explicit --squash is an invalid
  # combination on a queued branch: gh ERRORS ("The merge strategy for main is set by the merge
  # queue") instead of enqueueing. Confirmed live twice -- issue #3108, and again 2026-08-26 on
  # nonprofit-atlas#3307, which sat green and unmerged for hours with autoMergeRequest=null.
  #
  # And CHECK THE EXIT CODE. This call used to end in `>/dev/null 2>&1` with the "auto-merge
  # armed" line unconditionally after it -- so a failed arm logged as a successful one and the
  # PR simply never merged, with nothing anywhere saying why.
  if arm_err="$(gh pr merge "$PR_NUM" --auto 2>&1 >/dev/null)"; then
    log "item #$ITEM_ID: opened PR #$PR_NUM, auto-merge armed"
  else
    log "item #$ITEM_ID: opened PR #$PR_NUM, but ARMING AUTO-MERGE FAILED -- it will not merge on green: ${arm_err:-unknown error}"
  fi
else
  log "item #$ITEM_ID: build session ended with no PR URL found in output -- releasing claim"
fi
exit 0
