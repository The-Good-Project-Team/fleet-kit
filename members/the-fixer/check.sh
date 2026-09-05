#!/bin/bash
# check.sh — the-fixer's deterministic red-check tool. ZERO LLM spend: one `gh run list` per
# workflow, printed as one line the-fixer's own charter reads before deciding whether to act.
#
# Provenance: genericized from nonprofit-atlas's scripts/lucky2/firefighter.sh (real incident
# #2323, 2026-08-14: an 8-hour outage where CI and deploy stayed GREEN the entire time -- "did
# the build pass" is not "is the site up". The double-probe prod check and fire-once-per-SHA
# dedup below are load-bearing, not decoration; keep both if you touch this file).
#
# WHY A SEPARATE SCRIPT FROM THE CHARTER: the-fixer is an llm member (see member_spec.py's
# header, 2026-08-21) -- it has a goal and reaches for tools, it isn't a script. But polling
# `gh run list` through an LLM turn on every green tick is pure waste; this stays a cheap,
# deterministic Bash tool in the-fixer's own allowlist, called first, every pass, before any
# reasoning happens. The state/dedup/heartbeat logic here is unchanged from the old mechanical
# version -- only the "then run claude -p to fix it" tail moved OUT, into the charter + the
# member's normal run_member.sh invocation, so there is exactly one path that calls claude.
#
# Prints one line to stdout: "green" or "FIRE <what> <sha-prefix> [PROD_DOWN detail]".
# Exit 0 either way -- this tool reports state, it never itself decides success/failure.
set -uo pipefail

REPO="${FLEET_REPO:?set FLEET_REPO}"
LOG_DIR="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}"
LOG="$LOG_DIR/the-fixer.log"
# FIXER_STATE_FILE override exists so a human/agent can dry-run this script against a real PR
# to verify a fix (exactly what happened testing PR #46's own StatusContext detection fix,
# 2026-08-24) without corrupting the fleet's real dedup state -- before this, a manual run wrote
# "red <sha>" to the SAME file cron uses, so the next real the-fixer pass saw "already-fighting"
# and no-op'd on a fire nobody had actually fought yet. Verification runs now pass
# FIXER_STATE_FILE=/tmp/whatever; cron's real invocation is unaffected (falls through to the
# same default path as before).
#
# Default lives under $LOG_DIR, NOT $HOME/.cache -- confirmed live 2026-08-29 (fleet-kit
# gh#215): $HOME is the per-pass ephemeral overlay (a fresh worktree/container each run), while
# $FLEET_LOG_DIR is the one bind-mounted, cross-pass-persistent path (the-fixer.log itself has
# entries spanning days, proving it survives). A dedup file on $HOME/.cache silently resets
# between passes, so the SAME already-fought SHA (91eb91a, reverted via PR #188 at 04:52 UTC)
# re-fired as a fresh FIRE almost 11 hours later -- the exact "fire twice per SHA" bug this
# state file exists to prevent.
STATE="${FIXER_STATE_FILE:-$LOG_DIR/the-fixer.state}"
HB_STAMP="$LOG_DIR/the-fixer.hb"

mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE")"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

cd "$REPO" 2>/dev/null || { log "FATAL: repo missing at $REPO"; echo "FIRE repo-missing none"; exit 0; }

read_latest() { # <workflow> [branch] -> "conclusion sha"
  local wf="$1" branch="${2:-}"
  gh run list --workflow "$wf" ${branch:+--branch "$branch"} \
    --limit 5 --json conclusion,headSha \
    -q '[.[] | select(.conclusion != null and .conclusion != "" and .conclusion != "cancelled")][0] | (.conclusion // "none") + " " + (.headSha // "none")' \
    2>>"$LOG" || echo "unreadable none"
}

CI_STATE=$(read_latest "${FIXER_CI_WORKFLOW:-ci.yml}" "${FIXER_DEFAULT_BRANCH:-main}")
# A workflow file that's since been deleted from the default branch (e.g. reverted) can never
# produce a new run again -- but `gh run list` still returns its last historical run forever,
# so without this guard a single old failure (91eb91a9228c / PR #149, reverted via PR #188 at
# 04:52 UTC 2026-08-29) becomes an eternal false "failure" that re-fires FIRE every time the
# dedup state file is ever reset for any reason (confirmed live: it re-fired at 23:47 UTC the
# same day, 19h after the revert, despite the workflow file no longer existing in $REPO).
DEPLOY_WF="${FIXER_DEPLOY_WORKFLOW:-deploy.yml}"
if [ -f "$REPO/.github/workflows/$DEPLOY_WF" ]; then
  DEPLOY_STATE=$(read_latest "$DEPLOY_WF")
else
  DEPLOY_STATE="none none"
  log "deploy workflow $DEPLOY_WF not present in \$REPO -- ignoring its stale historical run"
fi
CI_CONC="${CI_STATE%% *}";      CI_SHA="${CI_STATE#* }"
DEP_CONC="${DEPLOY_STATE%% *}"; DEP_SHA="${DEPLOY_STATE#* }"

# main-only CI/deploy checks above miss a red PR branch entirely -- it never touches main, so
# it just sits BLOCKED forever with nobody watching (real case: PR #3071, RESUME_BRIEF.md fails
# its own docs-linter, filed 2026-08-21, never actioned).
#
# Reif, 2026-08-22: "if a PR is stuck, who fixes it... if we have N issues we can deploy N
# independent units" -- an EARLIER version of this only surfaced the single oldest stale PR, so
# a genuinely hard one sat first in line and every easier PR behind it waited its turn forever,
# even though nothing links them. List ALL stale PRs (not just the oldest); the-fixer's own
# charter decides how many to fight this pass and fans out one independent sub-pass per PR,
# same "orchestrator decides N, spawns one per unit" shape as gru's own minions
# (docs/gru-minions.md) -- these PRs share no state, there is no reason to serialize them.
#
# "FAILURE" is not the only way a PR gets stuck (Reif, 2026-08-22: "what happens if there's
# another type of failure -- is that covered"). Live proof case: PR #3059 has every check
# SUCCESS and is stuck purely on a merge conflict (mergeStateStatus=DIRTY) -- invisible to a
# FAILURE-only sweep. Three shapes checked, none of them a guess at a threshold that hasn't
# been seen fail yet:
#   1. a genuine FAILURE conclusion on any required check (already covered above)
#   2. mergeStateStatus == DIRTY -- a real merge conflict, no amount of waiting resolves it
#   3. a check still IN_PROGRESS/QUEUED past a generous age (default 2h) -- a hung runner or a
#      wedged job never posts a conclusion at all, so it can sit "pending" forever with nothing
#      ever going red. STALE_PENDING_HOURS is deliberately coarse (a 90-minute e2e suite is not
#      stuck at minute 91) -- this catches "still running after lunch," not "running long."
#   4. a non-draft PR past the same age with ZERO checks in the rollup -- "no-checks-at-all".
#   5. a check whose conclusion is STARTUP_FAILURE/ACTION_REQUIRED/STALE, or state ERROR --
#      "check-never-ran".
#
# SHAPES 4 AND 5 ARE THE "NO ANSWER" CLASS, and they are why this sweep missed real PRs.
# The merge gate is binary -- it asks "is the required check green?" and arms on GREEN or
# alarms on RED. There is a third outcome it was never built for: NEITHER. A workflow that
# dies before its jobs launch (`startup_failure`) posts no check at all, and a workflow that
# never triggers posts nothing either. The PR then sits BLOCKED forever: it cannot merge
# (a required check is missing) and cannot alarm (nothing went red). Silent, indefinite.
#
# None of shapes 1-3 catch it, and each for its own reason:
#   $failed  -- the conclusion is STARTUP_FAILURE, not FAILURE.
#   $conflict-- the branch merges cleanly; nothing is DIRTY.
#   $wedged  -- requires status != COMPLETED, and a startup failure IS completed. It completed
#               by dying. A run that never started is not in the rollup at all, so every
#               filter that iterates rollup entries has nothing to iterate.
#
# Measured live 2026-08-26: of 10 open PRs, THREE non-draft ones (#3298, #3307, #3308) were
# BLOCKED with zero checks, aged 1-2h, while #3306/#3309 sat CLEAN with 3 checks each -- so
# checks do fire on this repo; these shas simply never got a run (`gh run list` returns zero
# runs for them, not a failed one). Separately, 2 of the last 25 workflow runs concluded
# `startup_failure`. Both halves of the class are real and concurrent.
#
# Draft PRs are excluded from shape 4 deliberately: a draft with no checks is normal (many
# workflows skip drafts on purpose), so alarming on it would be noise on every WIP branch.
# Both shapes reuse the SAME staleness cutoff as $wedged rather than inventing a threshold --
# a PR opened 90 seconds ago legitimately has no checks yet.
#
# A FAILURE conclusion only ever comes back on GitHub Actions checks (typename CheckRun).
# judge-judy's own fleet-code-review gate posts through the legacy commit-status API instead
# (typename StatusContext) -- same red X on the PR, but its failure lands in the `state` field,
# never `conclusion`. Live proof case: PR #3127, judge-judy BLOCKed it 4+ hours, every shape
# checked here still read $failed=false the whole time (Reif, 2026-08-24, driving fleet
# unattended -- traced check.sh live, reproduced the miss, confirmed the schema split by
# diffing gh pr view --json statusCheckRollup for a CheckRun vs a StatusContext entry). Both
# fields are checked below now, not just one.
# SHAPE 6 IS THE "DONE BUT NOT DELIVERED" CLASS -- green, mergeable, and going nowhere.
#
# Shapes 1-5 all key on RED or ABSENT: something failed, conflicted, hung, errored, or never
# reported. There is a sixth outcome none of them can see -- every check PASSED, the branch is
# mergeable, and the PR still sits open forever because nothing ever armed auto-merge on it.
# fleet-kit arms auto-merge in exactly ONE place (worktree_builder.sh, at PR-creation time), so
# a PR opened by a human, an external agent, or a hand-pushed branch is never armed at all.
#
# Live proof case (fleet-kit#291, 2026-09-02): judge-judy BLOCKed it at 15:30, auto_update_branch
# rebased it, judge-judy re-reviewed at 15:47 and posted fleet-code-review=SUCCESS. selftest
# green, mergeStateStatus CLEAN, automerge=none. The whole self-heal loop ran end to end and
# stopped one step short of done -- silently, with nothing red anywhere for shapes 1-5 to find.
# auto_update_branch.sh now arms these on a schedule, but an arm can itself fail (auto-merge
# disabled on the repo, insufficient token scope) and that failure is only a log line. This
# shape is the alarm for "the fix that was supposed to unstick it did not".
#
# Gated on the SAME staleness cutoff as the other quiet shapes: a PR that went green 90 seconds
# ago is not parked, it is just new, and the arming sweep runs every 15 minutes. UNSTABLE counts
# alongside CLEAN because a non-required check being red still leaves a PR mergeable -- required
# checks are what gate the merge, and a genuinely failing required check is already shape 1.
STALE_PENDING_HOURS="${FIXER_STALE_PENDING_HOURS:-2}"
# `gh pr list --json`/`gh pr view --json` have no `mergeQueueEntry` field at all (confirmed
# live, fleet-kit gh#4305) -- autoMergeRequest alone can't tell "never armed" from "already
# enqueued", since a queue entry doesn't populate it. Fetching mergeQueueEntry needs a raw
# GraphQL call, aliased per PR number so a batch of candidates costs one round trip, not N.
queued_prs() { # <space-separated pr numbers> -> the subset that already has a mergeQueueEntry
  local nums="$1" owner name query n
  [ -z "$nums" ] && return
  read -r owner name < <(gh repo view --json owner,name -q '.owner.login + " " + .name' 2>>"$LOG")
  [ -z "$owner" ] && return
  query="query {"
  for n in $nums; do
    query+=" pr$n: repository(owner: \"$owner\", name: \"$name\") { pullRequest(number: $n) { mergeQueueEntry { state } } }"
  done
  query+=" }"
  timeout 25s gh api graphql -f query="$query" \
    -q '.data | to_entries[] | select(.value.pullRequest.mergeQueueEntry != null) | .key | ltrimstr("pr")' \
    2>>"$LOG"
}
read_stale_prs() { # -> space-separated "num:sha:reason" triples, oldest first, or nothing
  # `gh ... -q/--jq` is a plain expression string, NOT the real jq CLI -- it has no --arg flag
  # to bind the cutoff safely, so it's computed here and interpolated as a quoted ISO-8601
  # literal into the expression itself (a timestamp string, not attacker-controlled input).
  local cutoff raw candidates queued entry num reason filtered=()
  cutoff=$(date -u -d "-${STALE_PENDING_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
           || date -u -v-"${STALE_PENDING_HOURS}"H +%Y-%m-%dT%H:%M:%SZ)
  raw=$(gh pr list --state open --limit 30 \
    --json number,headRefOid,mergeStateStatus,statusCheckRollup,isDraft,autoMergeRequest,updatedAt \
    -q '
      sort_by(.number) | .[] |
      ( [.statusCheckRollup[]? | select(.conclusion == "FAILURE" or .state == "FAILURE")] | length > 0 ) as $failed |
      ( .mergeStateStatus == "DIRTY" ) as $conflict |
      ( [.statusCheckRollup[]?
          | select(.status != null and .status != "COMPLETED" and .startedAt != null and .startedAt < "'"$cutoff"'")
        ] | length > 0 ) as $wedged |
      ( (.isDraft | not) and ([.statusCheckRollup[]?] | length == 0)
        and .createdAt < "'"$cutoff"'" ) as $noanswer |
      ( [.statusCheckRollup[]?
          | select(.conclusion == "STARTUP_FAILURE" or .conclusion == "ACTION_REQUIRED"
                   or .conclusion == "STALE" or .state == "ERROR")
        ] | length > 0 ) as $noran |
      ( (.isDraft | not) and (.autoMergeRequest == null)
        and (.mergeStateStatus == "CLEAN" or .mergeStateStatus == "UNSTABLE")
        and ([.statusCheckRollup[]?] | length > 0)
        and ([.statusCheckRollup[]? | select(.conclusion == "FAILURE" or .state == "FAILURE")] | length == 0)
        and .updatedAt < "'"$cutoff"'" ) as $parked |
      select($failed or $conflict or $wedged or $noanswer or $noran or $parked) |
      "\(.number):\(.headRefOid):\(if $failed then "check-failed" elif $conflict then "merge-conflict" elif $wedged then "wedged-check" elif $noran then "check-never-ran" elif $parked then "green-but-parked" else "no-checks-at-all" end)"
    ' 2>>"$LOG")

  candidates=""
  for entry in $raw; do
    [ "${entry##*:}" = "green-but-parked" ] && candidates="$candidates ${entry%%:*}"
  done
  queued=$(queued_prs "$candidates")

  for entry in $raw; do
    num="${entry%%:*}"; reason="${entry##*:}"
    if [ "$reason" = "green-but-parked" ] && grep -qx "$num" <<<"$queued"; then
      continue  # already enqueued -- not parked, regardless of autoMergeRequest (gh#4305)
    fi
    filtered+=("$entry")
  done
  [ "${#filtered[@]}" -gt 0 ] && printf '%s ' "${filtered[@]}"
}
STALE_PRS=$(read_stale_prs)
# The dedup key for a stale-PR fire is the WHOLE batch, not the oldest PR's sha.
#
# It used to be the oldest sha alone, and that silently blinded the fleet for 11 hours
# (nonprofit-atlas, 2026-09-02): the 02:46 UTC pass fired on four PRs
# (3853:check-failed 3855:check-failed 3860:check-failed 3863:no-checks-at-all), fanned out a
# sub-pass each, and fixed three of them. #3853 was a merge conflict nobody resolved, so its
# head stayed 61a3390 forever -- and because the state file only ever held THAT sha, all ten
# subsequent hourly passes read "already-fighting 61a3390" and stopped at Step 1 without ever
# re-listing open PRs. Every PR that went red after 02:46 was invisible, and the-fixer reported
# "green" through all of it. One permanently-stuck PR must never silence the alarm for the
# others: it is exactly the "N independent units" reasoning the fanout itself is built on
# (Reif, 2026-08-22) -- serializing dedup behind the oldest unit re-introduces the head-of-line
# block that fanout exists to remove.
#
# Keying on the full "num:sha:reason" set means a batch re-fires whenever ANY member changes:
# a PR fixed and dropping out, a new PR going red, or a stuck PR's own head moving. A batch
# genuinely unchanged tick-over-tick still dedupes exactly as before, so a hard PR mid-fix is
# not re-fought every hour.
FIRST_PR="${STALE_PRS%% *}"
PR_NUM="${FIRST_PR%%:*}"
PR_REST="${FIRST_PR#*:}"; PR_SHA="${PR_REST%%:*}"
[ -z "$STALE_PRS" ] && PR_NUM="none"
# Whitespace-normalized so trailing-space noise from read_stale_prs (already sorted by PR
# number, so ordering is stable) can't spuriously re-fire an identical batch.
STALE_KEY=$(printf '%s' "$STALE_PRS" | tr -s ' ' ' ' | sed 's/^ *//; s/ *$//')

# Optional: is the deployed product actually serving? Set FIXER_HEALTH_URL/FIXER_PAGE_URL to
# enable. Both must fail before this counts as a fire -- a single timeout is a blip, not an
# outage.
PROD_DOWN=""
if [ -n "${FIXER_HEALTH_URL:-}" ] && [ -n "${FIXER_PAGE_URL:-}" ]; then
  probe() {
    local code
    code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0 (fleet-kit the-fixer)' "$1" 2>/dev/null)
    [ -z "$code" ] && code=000
    printf '%s' "$code"
  }
  P_HEALTH=$(probe "$FIXER_HEALTH_URL")
  case "$P_HEALTH" in 2*|3*)
    P_PAGE=$(probe "$FIXER_PAGE_URL")
    case "$P_PAGE" in 2*|3*) ;; *)
      P_PAGE2=$(probe "$FIXER_PAGE_URL")   # confirm: never fire on one sample
      case "$P_PAGE2" in 2*|3*) ;; *) PROD_DOWN="health=$P_HEALTH page=$P_PAGE,$P_PAGE2" ;; esac
    esac ;;
  *) PROD_DOWN="health=$P_HEALTH" ;;
  esac
fi

FIRE_SHA=""
FIRE_WHAT=""
if [ "$DEP_CONC" = "failure" ]; then FIRE_SHA="$DEP_SHA"; FIRE_WHAT="${FIXER_DEPLOY_WORKFLOW:-deploy.yml}"; fi
if [ "$CI_CONC" = "failure" ]; then FIRE_SHA="$CI_SHA"; FIRE_WHAT="${FIRE_WHAT:+$FIRE_WHAT+}${FIXER_CI_WORKFLOW:-ci.yml}(${FIXER_DEFAULT_BRANCH:-main})"; fi
if [ "$PR_NUM" != "none" ] && [ -z "$FIRE_SHA" ]; then
  # main/deploy fires outrank stale PRs -- a red main is the bigger emergency either way.
  # FIRE_WHAT carries every stale PR (num:sha pairs), not just one -- the-fixer's charter fans
  # out a sub-pass per PR named here rather than fighting one and leaving the rest queued.
  # Dedup on the whole batch (see STALE_KEY above); $PR_SHA alone let one stuck PR mute the
  # rest. FIRE_SHA is what the state file stores, so it carries the batch key; the human-facing
  # log line below still prints the oldest sha's prefix for continuity.
  FIRE_SHA="batch:$STALE_KEY"; FIRE_WHAT="stale-prs($STALE_PRS)"
fi
if [ -n "$PROD_DOWN" ]; then
  # No single commit is necessarily guilty (an outage can be a resource threshold crossed, not
  # a bad deploy) -- bucket by time so dedup has something stable while a live outage re-fires.
  FIRE_SHA="prod-$(( $(date +%s) / 1800 ))"
  FIRE_WHAT="PROD DOWN ($PROD_DOWN)"
fi

# 6-hourly liveness so a ghost/deadman audit can tell "quiet" from "dead".
if [ ! -f "$HB_STAMP" ] || [ -n "$(find "$HB_STAMP" -mmin +360 2>/dev/null)" ]; then
  touch "$HB_STAMP"
  log "alive -- last check ci=$CI_CONC deploy=$DEP_CONC (quiet ticks emit nothing else)"
fi

LAST=$(cat "$STATE" 2>/dev/null || echo "")

if [ -z "$FIRE_SHA" ]; then
  [ -n "$LAST" ] && [ "${LAST%% *}" = "red" ] && log "EXTINGUISHED: green again (ci=$CI_CONC deploy=$DEP_CONC)"
  echo "green $CI_SHA" > "$STATE"
  echo "green"
  exit 0
fi

# A dedup that can never expire is a dedup that can wedge permanently. The batch key above
# fixes the common case (any PR entering/leaving the batch re-fires), but a batch that is
# genuinely unchanged -- one stuck PR, nothing else red -- would still suppress forever, and
# "forever" is how the 11-hour blind spot lasted 11 hours instead of one. After
# FIXER_DEDUP_MAX_HOURS (default 6) on the SAME key, fire again regardless: whoever was fixing
# it either finished, gave up, or died, and all three deserve a fresh look. The state file's
# mtime is the clock -- it is rewritten on every state change, so it measures "how long has
# this exact fire been unresolved", which is the question being asked.
DEDUP_MAX_HOURS="${FIXER_DEDUP_MAX_HOURS:-6}"
if [ "$LAST" = "red $FIRE_SHA" ]; then
  if [ -n "$(find "$STATE" -mmin +$(( DEDUP_MAX_HOURS * 60 )) 2>/dev/null)" ]; then
    log "DEDUP EXPIRED: same fire unresolved >${DEDUP_MAX_HOURS}h ($FIRE_SHA) -- re-firing"
    touch "$STATE"   # restart the clock so it re-fires every N hours, not every tick after N
    echo "FIRE $FIRE_WHAT ${FIRE_SHA:0:12}"
    exit 0
  fi
  log "still red at $FIRE_SHA -- already fought this head, waiting for the fix PR / a new sha"
  echo "green (already-fighting $FIRE_SHA)"
  exit 0
fi

log "FIRE: $FIRE_WHAT failure at ${FIRE_SHA:0:12}"
echo "red $FIRE_SHA" > "$STATE"
echo "FIRE $FIRE_WHAT ${FIRE_SHA:0:12}"
