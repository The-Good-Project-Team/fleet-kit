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
LOG="${FLEET_LOG_DIR:-$HOME/Library/Logs/fleet-kit}/the-fixer.log"
STATE="$HOME/.cache/fleet-kit/the-fixer.state"
HB_STAMP="$HOME/.cache/fleet-kit/the-fixer.hb"

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
DEPLOY_STATE=$(read_latest "${FIXER_DEPLOY_WORKFLOW:-deploy.yml}")
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
STALE_PENDING_HOURS="${FIXER_STALE_PENDING_HOURS:-2}"
read_stale_prs() { # -> space-separated "num:sha:reason" triples, oldest first, or nothing
  # `gh ... -q/--jq` is a plain expression string, NOT the real jq CLI -- it has no --arg flag
  # to bind the cutoff safely, so it's computed here and interpolated as a quoted ISO-8601
  # literal into the expression itself (a timestamp string, not attacker-controlled input).
  local cutoff
  cutoff=$(date -u -d "-${STALE_PENDING_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
           || date -u -v-"${STALE_PENDING_HOURS}"H +%Y-%m-%dT%H:%M:%SZ)
  gh pr list --state open --limit 30 \
    --json number,headRefOid,mergeStateStatus,statusCheckRollup \
    -q '
      sort_by(.number) | .[] |
      ( [.statusCheckRollup[]? | select(.conclusion == "FAILURE")] | length > 0 ) as $failed |
      ( .mergeStateStatus == "DIRTY" ) as $conflict |
      ( [.statusCheckRollup[]?
          | select(.status != null and .status != "COMPLETED" and .startedAt != null and .startedAt < "'"$cutoff"'")
        ] | length > 0 ) as $wedged |
      select($failed or $conflict or $wedged) |
      "\(.number):\(.headRefOid):\(if $failed then "check-failed" elif $conflict then "merge-conflict" else "wedged-check" end)"
    ' 2>>"$LOG" | tr '\n' ' '
}
STALE_PRS=$(read_stale_prs)
# Kept for the single-value fire-dedup state file below: the OLDEST stale PR's sha is still
# what "already-fighting" dedupes against, so a run that only ever fixes the oldest one doesn't
# re-fire every tick on the same head -- but FIRE_WHAT below now names every stale PR (with each
# one's own reason), not just that one, so the-fixer's own pass sees the whole list to fan out
# over. Each entry is "num:sha:reason"; only the first entry's num/sha feed the dedup state.
FIRST_PR="${STALE_PRS%% *}"
PR_NUM="${FIRST_PR%%:*}"
PR_REST="${FIRST_PR#*:}"; PR_SHA="${PR_REST%%:*}"
[ -z "$STALE_PRS" ] && PR_NUM="none"

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
  FIRE_SHA="$PR_SHA"; FIRE_WHAT="stale-prs($STALE_PRS)"
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

if [ "$LAST" = "red $FIRE_SHA" ]; then
  log "still red at $FIRE_SHA -- already fought this head, waiting for the fix PR / a new sha"
  echo "green (already-fighting $FIRE_SHA)"
  exit 0
fi

log "FIRE: $FIRE_WHAT failure at ${FIRE_SHA:0:12}"
echo "red $FIRE_SHA" > "$STATE"
echo "FIRE $FIRE_WHAT ${FIRE_SHA:0:12}"
