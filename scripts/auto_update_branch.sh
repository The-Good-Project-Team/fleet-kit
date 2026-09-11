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

# shellcheck source=/dev/null
. "$KIT_DIR/scripts/merge_arm.sh"

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

# --- close stale (>48h) unarmed/red/queue-rejected PRs, free the claim --------------------
# fleet-kit#527 (the other half of #523's "nothing sits"): a PR that never arms -- red CI,
# blocked by fleet-code-review with no fix pushed since, or stuck cycling in and out of the
# merge queue -- can sit open indefinitely, and its issue's fleet:claimed label sits with it:
# invisible to gru (it only ever picks unclaimed items) and invisible to marie's Part A
# stale-claim check (marie.md: that only clears a claim with NO open PR at all, never one
# stuck behind a dead PR).
#
# Never a human-authored PR: `gh`'s own `author` field is useless here (every fleet-opened PR
# is pushed under the same authenticated account -- fleet_view_server.py's own "author is
# USELESS, the real signal is the branch name" note applies just as much here), so the guard
# is the branch-name convention run_member.sh actually stamps: `member/<worker>-item<N>-...`
# (see run_member.sh's WT_BRANCH). A hand-authored branch (`fix/...`, `feat/...`, etc.) never
# matches and is always skipped, never closed.
#
# Never a PR correctly waiting its turn in the queue: gh#4305 already burned this once --
# `autoMergeRequest` stays null for a PR that's already enqueued (the queue entry doesn't
# populate that field), so the only reliable signal is a raw GraphQL `mergeQueueEntry` call,
# same lesson members/the-fixer/check.sh's `queued_prs()` encodes. Reused here in the same
# batched-single-call shape (one round trip for every candidate, not N).
STALE_HOURS="${FLEET_STALE_PR_HOURS:-48}"
STALE_CUTOFF=$(date -u -d "-${STALE_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
               || date -u -v-"${STALE_HOURS}"H +%Y-%m-%dT%H:%M:%SZ)
CLOSED=0

queued_prs() { # <space-separated pr numbers> -> the subset that already has a mergeQueueEntry
  local nums="$1" owner name query n
  [ -z "$nums" ] && return
  owner="${REPO_SLUG%%/*}"; name="${REPO_SLUG#*/}"
  query="query {"
  for n in $nums; do
    query+=" pr$n: repository(owner: \"$owner\", name: \"$name\") { pullRequest(number: $n) { mergeQueueEntry { state } } }"
  done
  query+=" }"
  timeout 25s gh api graphql -f query="$query" \
    -q '.data | to_entries[] | select(.value.pullRequest.mergeQueueEntry != null) | .key | ltrimstr("pr")' \
    2>/dev/null
}

STALE_CANDIDATES=$(gh pr list --state open --json number,isDraft,headRefName,createdAt \
  -q '.[] | select(.isDraft|not) | select(.headRefName | test("^(member|minion)/")) | select(.createdAt < "'"$STALE_CUTOFF"'") | .number' 2>/dev/null)
STALE_QUEUED=$(queued_prs "$STALE_CANDIDATES")

for pr in $STALE_CANDIDATES; do
  if grep -qx "$pr" <<<"$STALE_QUEUED"; then
    log "PR #$pr: not closing -- correctly waiting its turn in the merge queue (gh#4305)"
    continue
  fi

  # A genuine push (a real fix, not this same script's own branch-sync merge) resets the
  # clock: closing right after someone pushed a fix would race the next CI/review cycle.
  # Filter out `update-branch`'s own "Merge branch ... into ..." sync commits so a PR this
  # script keeps rebasing every tick (the loop below) doesn't look permanently "fresh" even
  # though nobody has touched its actual content in days.
  last_real_commit=$(gh pr view "$pr" --json commits \
    -q '[.commits[] | select((.messageHeadline | startswith("Merge branch")) and (.messageHeadline | contains("into")) | not)] | if length>0 then .[-1].committedDate else empty end' \
    2>/dev/null)
  if [ -n "$last_real_commit" ] && [[ "$last_real_commit" > "$STALE_CUTOFF" ]]; then
    log "PR #$pr: not closing -- a real commit landed at $last_real_commit, inside the window"
    continue
  fi

  head=$(gh pr view "$pr" --json headRefOid -q '.headRefOid' 2>/dev/null)
  rollup_failed=$(gh pr view "$pr" --json statusCheckRollup \
    -q '[.statusCheckRollup[]? | select(.conclusion=="FAILURE" or .state=="FAILURE")] | length' 2>/dev/null)
  verdict=$(timeout 25s gh api "repos/${REPO_SLUG}/statuses/${head}" --jq '[.[] | select(.context=="fleet-code-review")][0].state' 2>/dev/null)
  # `grep -c` itself always prints a count (even "0") but exits 1 on no match, so it can't be
  # chained with `|| echo 0` without double-printing -- the fallback only kicks in when the
  # LOG file is absent.
  armed_count=0
  [ -f "$LOG" ] && armed_count=$(grep -c "PR #$pr: auto-merge armed" "$LOG" 2>/dev/null)
  armed_count="${armed_count:-0}"

  if [ "${rollup_failed:-0}" -gt 0 ] 2>/dev/null || [ "$verdict" = "failure" ] || [ "$verdict" = "error" ]; then
    reason="red"
  elif [ "${armed_count:-0}" -ge 2 ] 2>/dev/null; then
    reason="${armed_count} queue rejections"
  else
    reason="unarmed"
  fi

  note="Closing: open >${STALE_HOURS}h with no follow-up push and never merged ($reason, as of $(ts)). Nothing here is a content judgment -- the fleet-code-review/CI gates are what decided this never went green. A human or a future minion pass can pick this issue back up fresh."
  if gh pr close "$pr" --comment "$note" >/dev/null 2>&1; then
    log "PR #$pr: closed stale PR ($reason, created before $STALE_CUTOFF)"
    CLOSED=$((CLOSED+1))

    # Free the issue's claim so gru can re-pick it -- the whole point of this closing path.
    # Backlog:# is worktree_builder.sh's own stamped convention (its body-append step); the
    # branch's own -itemN- segment (run_member.sh's WT_BRANCH shape) is the fallback for a
    # body that was hand-edited or never got stamped.
    body=$(gh pr view "$pr" --json body -q '.body' 2>/dev/null)
    issue=$(grep -oE 'Backlog:[[:space:]]*#[0-9]+' <<<"$body" | grep -oE '[0-9]+' | head -1)
    if [ -z "$issue" ]; then
      pr_head=$(gh pr view "$pr" --json headRefName -q '.headRefName' 2>/dev/null)
      issue=$(sed -nE 's#.*-item([0-9]+)-.*#\1#p' <<<"$pr_head")
    fi
    if [ -n "$issue" ]; then
      if python3 "$KIT_DIR/scripts/board_github.py" release "$issue" \
           "released by auto_update_branch.sh: PR #$pr closed stale ($reason), re-claimable" >>"$LOG" 2>&1; then
        log "issue #$issue: fleet:claimed removed (PR #$pr closed stale)"
      else
        log "issue #$issue: board_github.py release FAILED -- claim may still be stuck"
      fi
    else
      log "PR #$pr: closed but could not determine its originating issue -- claim label untouched"
    fi
  else
    log "PR #$pr: close FAILED"
  fi
done

UPDATED=0
CHECKED=0
for pr in $(gh pr list --state open --json number,isDraft,mergeable,mergeStateStatus \
              -q '.[] | select(.isDraft|not) | select(.mergeable=="MERGEABLE") | select(.mergeStateStatus=="BEHIND" or .mergeStateStatus=="BLOCKED") | .number' 2>/dev/null); do
  CHECKED=$((CHECKED+1))
  head_ref=$(gh pr view "$pr" --json headRefName -q '.headRefName' 2>/dev/null)
  [ -z "$head_ref" ] && continue
  behind_by=$(timeout 25s gh api "repos/${REPO_SLUG}/compare/main...${head_ref}" --jq '.behind_by' 2>/dev/null || echo 0)
  [ "${behind_by:-0}" -le 0 ] && continue
  if timeout 25s gh api -X PUT "repos/${REPO_SLUG}/pulls/${pr}/update-branch" >/dev/null 2>&1; then
    log "PR #$pr: updated branch (was $behind_by commit(s) behind main)"
    UPDATED=$((UPDATED+1))
  else
    log "PR #$pr: update-branch call failed"
  fi
done

# --- arm auto-merge on any green-able PR that lacks it --------------------------------------
# The loop above keeps branches current; judge-judy re-reviews them and turns fleet-code-review
# green. Nothing then MERGES the result, because auto-merge is armed in exactly one place in
# this repo -- worktree_builder.sh, at PR-creation time (`gh pr merge --auto`). A PR opened by
# anything else (a human, an external agent, a hand-pushed branch) is never armed, so it can
# go fully green and sit open forever: no error, no alarm, nothing red for the-fixer to find.
#
# Live case that motivated this (fleet-kit#291, 2026-09-02): judge-judy BLOCKed it at 15:30,
# this script's update-branch loop rebased it, judge-judy re-reviewed at 15:47 and posted
# fleet-code-review=SUCCESS. selftest green, mergeStateStatus CLEAN, automerge=none -- the
# self-heal loop ran end to end and still stopped one step short of done, permanently.
# the-fixer cannot see it either: its sweep hunts red and "no answer", and this PR is green.
#
# Arming is NOT merging, so this keeps the file header's promise that this script never merges
# a PR itself and needs no content judgment: GitHub merges an armed PR only once every REQUIRED
# check passes, so judge-judy's fleet-code-review gate still decides. Arming a PR that is red
# or unreviewed simply parks it -- it waits, exactly as an armed member-opened PR does.
#
# Draft PRs are excluded (a draft is explicitly "not ready"), and so is anything already armed
# -- re-arming is a no-op API call, but skipping it keeps the log honest about what changed.
ARMED=0
for pr in $(gh pr list --state open --json number,isDraft,autoMergeRequest \
              -q '.[] | select(.isDraft|not) | select(.autoMergeRequest==null) | .number' 2>/dev/null); do
  # fleet-kit#523: never re-arm a head judge-judy blocked. fleet-code-review is not a required
  # check under the merge queue, so an armed BLOCKed PR simply merges. Newest status first.
  # gh#806: "error" (judge-judy gave up after MAX_PARSE_STRIKES schema-invalid runs) holds the
  # same as "failure" -- judge-judy.sh itself already disarms via unqueue_pr the moment it
  # posts state=error, but this guard existed to stop a LATER re-arm from undoing that, and it
  # used to only recognize "failure", so an errored head could still slip back through here.
  head=$(gh pr view "$pr" --json headRefOid -q '.headRefOid' 2>/dev/null)
  verdict=$(timeout 25s gh api "repos/${REPO_SLUG}/statuses/${head}" --jq '[.[] | select(.context=="fleet-code-review")][0].state' 2>/dev/null || true)
  if [ "$verdict" = "failure" ] || [ "$verdict" = "error" ]; then
    log "PR #$pr: not armed -- judge-judy blocked or errored this head (${head:0:12}, state=$verdict)"
    continue
  fi
  if arm_err="$(arm_pr_auto_merge "$pr")"; then
    log "PR #$pr: auto-merge armed (was unarmed -- it could have sat green forever)"
    ARMED=$((ARMED+1))
  else
    # Never fatal: a PR can be unarmable for legitimate reasons (auto-merge disabled on the
    # repo, insufficient permissions, already merged between the list and this call). Log the
    # real reason rather than a silent skip -- worktree_builder.sh#231 learned that one the
    # hard way, logging "armed" unconditionally while the arm had actually failed.
    log "PR #$pr: could not arm auto-merge: ${arm_err:-unknown error}"
  fi
done

log "tick done: checked $CHECKED PR(s), updated $UPDATED, armed $ARMED, closed stale $CLOSED"
