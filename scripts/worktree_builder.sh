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

[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && . "${FLEET_ENV_FILE:-./fleet.env}"

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

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }
[ -f "$KIT_DIR/scripts/account_pool.sh" ] && . "$KIT_DIR/scripts/account_pool.sh"
command -v account_pool_run >/dev/null 2>&1 || account_pool_run() { "$@"; }

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
cleanup() { git -C "$REPO" worktree remove --force "$WT_PATH" >/dev/null 2>&1 || true; git -C "$REPO" worktree prune >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- STEP 3: build ------------------------------------------------------------------------------
CHARTER=""
[ -f "$KIT_DIR/agents/builder.md" ] && CHARTER=$(awk 'BEGIN{d=0} /^---$/{d++; next} d>=2{print}' "$KIT_DIR/agents/builder.md")
PROMPT="$CHARTER

## This item (injected by worktree_builder.sh)
ID: $ITEM_ID
Title: $ITEM_TEXT
Context: $ITEM_CONTEXT

You are in a fresh worktree at $WT_PATH on branch $WT_BRANCH. Commit + push from here."

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
OUT=$(cd "$WT_PATH" && account_pool_run timeout "$TIMEOUT_S" claude -p "$PROMPT" \
  --model "$MODEL" --dangerously-skip-permissions --max-turns "$MAX_TURNS" 2>>"$LOG")
RC=$?

if [ "$RC" -ne 0 ]; then
  log "item #$ITEM_ID: build session failed rc=$RC (account=${ACCOUNT_POOL_SELECTED:-none})"
  exit 1
fi

# --- STEP 4: stamp the backlog id onto the PR (idempotent) --------------------------------------
# Same lesson as the source fleet: a prose "mention the item ID" instruction has a real-world
# compliance rate well under 100%. Stamp it deterministically here rather than hoping.
PR_NUM=$(grep -oE 'github\.com/[^ ]+/pull/[0-9]+' <<<"$OUT" | tail -1 | grep -oE '[0-9]+$')
if [ -n "$PR_NUM" ]; then
  BODY=$(gh pr view "$PR_NUM" --json body -q '.body' 2>/dev/null || echo "")
  if ! grep -qE 'Backlog:[[:space:]]*#[0-9]+' <<<"$BODY"; then
    printf '%s\n\nBacklog: #%s\n' "$BODY" "$ITEM_ID" | gh pr edit "$PR_NUM" --body-file - >/dev/null 2>&1
  fi
  gh pr merge "$PR_NUM" --auto --squash >/dev/null 2>&1
  log "item #$ITEM_ID: opened PR #$PR_NUM, auto-merge armed"
else
  log "item #$ITEM_ID: build session ended with no PR URL found in output"
fi
exit 0
