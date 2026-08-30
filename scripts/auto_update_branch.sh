#!/bin/bash
# auto_update_branch.sh — closes fleet-kit gh#172's root cause: nothing in this repo keeps an
# open PR's branch current with `main`, so as soon as ANY PR merges, every other open PR goes
# BEHIND/BLOCKED and stays there forever — its checks passed against a base that no longer
# exists, and GitHub's own auto-merge does not update branches itself, it only merges once
# already mergeable. Confirmed live 2026-08-29/30: gh#172 recurred 14+ times over multiple
# days, culminating in "full saturation" (8/8 open PRs simultaneously BEHIND, jefe's 00:25 UTC
# comment on that issue) with the only remedy being a human/jefe hand-running
# `gh api update-branch` per PR, one at a time, once a PR happened to sit fully green for 2h+.
#
# This script is that same remedy, run on a schedule instead of waiting for someone to notice.
# It only calls update-branch (a git merge of main into the PR branch) — it NEVER merges a PR
# itself, so it needs no content judgment and carries none of jefe's merge-exception carve-outs
# (jefe.md's "never touch a PR that edits merge-gate machinery" rule is about the MERGE action,
# not about keeping a branch in sync so its CI is meaningful).
#
# Cadence is picked to run just ahead of judge-judy's own :00/15/30/45 review tick (see
# entrypoint.sh) — a branch updated at :05 has its checks re-run in time for judge-judy's :15
# pick, instead of judge-judy also needing a 15-min wait AFTER the branch catches up.
set -uo pipefail

[ -f "${FLEET_ENV_FILE:-./fleet.env}" ] && . "${FLEET_ENV_FILE:-./fleet.env}"

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${FLEET_REPO:?set FLEET_REPO in fleet.env}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
LOG="$LOG_DIR/auto_update_branch.log"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# shellcheck source=/dev/null
[ -f "$KIT_DIR/scripts/fleet_enabled.sh" ] && . "$KIT_DIR/scripts/fleet_enabled.sh"
fleet_enabled_or_exit "auto_update_branch"

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; exit 1; }

REPO_SLUG=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")
[ -z "$REPO_SLUG" ] && { log "FATAL: cannot resolve repo slug (gh auth?)"; exit 1; }

# Single-tick mutex under $LOG_DIR (not $HOME/.cache) -- same reasoning as judge-judy.sh's own
# lock (fleet-kit#207): $HOME is the per-pass ephemeral container/worktree and can never block
# a concurrent tick running in a different one.
LOCKFILE="$LOG_DIR/auto_update_branch.lock"
exec 9>"$LOCKFILE"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  log "another tick still holds $LOCKFILE -- exiting"
  exit 0
fi

UPDATED=0
CHECKED=0
for pr in $(gh pr list --state open --json number,isDraft,mergeable,mergeStateStatus \
              -q '.[] | select(.isDraft|not) | select(.mergeable=="MERGEABLE") | select(.mergeStateStatus=="BEHIND" or .mergeStateStatus=="BLOCKED") | .number' 2>/dev/null); do
  CHECKED=$((CHECKED+1))
  head_ref=$(gh pr view "$pr" --json headRefName -q '.headRefName' 2>/dev/null)
  [ -z "$head_ref" ] && continue
  behind_by=$(gh api "repos/${REPO_SLUG}/compare/main...${head_ref}" --jq '.behind_by' 2>/dev/null || echo 0)
  [ "${behind_by:-0}" -le 0 ] && continue
  if gh api -X PUT "repos/${REPO_SLUG}/pulls/${pr}/update-branch" >/dev/null 2>&1; then
    log "PR #$pr: updated branch (was $behind_by commit(s) behind main)"
    UPDATED=$((UPDATED+1))
  else
    log "PR #$pr: update-branch call failed"
  fi
done
log "tick done: checked $CHECKED PR(s), updated $UPDATED"
