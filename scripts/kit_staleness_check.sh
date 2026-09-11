#!/bin/bash
# kit_staleness_check.sh -- gh#731: a read-side signal that /fleet-kit's baked snapshot has
# fallen behind origin/main, printed where a member pass actually looks (its own log) --
# not only where up.sh's build-time warning already looks (a human running a build by hand).
#
# gh#638 burned four passes because nothing told the member repro-ing a fix that
# scripts/lane_registry_lanes.py under /fleet-kit was 18 commits and ~6 hours stale: three
# "still broken, fresh direct repro" comments each ran the baked snapshot, not main, and each
# read as strong evidence precisely because a repro that "reproduces" is the confidence-building
# kind of wrong. `.deploy_sha` (Dockerfile:107) has carried the answer since gh#201; nothing
# ever read it back for this.
#
# Deliberately NOT deploy_staleness_check.sh (gh#201): that script is a fleet-wide ops watchdog
# on its own hourly cron, gated by a multi-hour paging budget so routine deploy lag doesn't page
# a human. This is the opposite shape on purpose -- no budget, N>0 commits is worth a line,
# because the audience is a member mid-repro deciding whether to trust what it just read, not an
# on-call human deciding whether to escalate. See run_member.sh's wiring for the other half: this
# script only prints, run_member.sh's preamble is what puts the line in a real pass's log.
#
# Advisory only, per this issue's own non-goals: never blocks, never a nonzero exit, regardless
# of network/auth/git state. Silent means "nothing worth saying", not "definitely current" --
# the missing-.deploy_sha, unknown-SHA and unreachable-remote branches below are all silent by
# design (AC3/AC4), not an oversight.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}" ] && { set -a; . "${FLEET_ENV_FILE:-$KIT_DIR/fleet.env}"; set +a; } || true

# Overridable so a test (or a non-standard layout) can point this at a fixture instead of the
# real baked file -- same idea as KIT_REPO_SLUG below.
DEPLOY_SHA_FILE="${KIT_STALENESS_DEPLOY_SHA_FILE:-$KIT_DIR/.deploy_sha}"

# Same inline derivation deploy_staleness_check.sh and self_improve_score.sh both use, and
# deliberately not factored into a shared lib -- see deploy_staleness_check.sh's own comment on
# why a shared file for one more one-line caller would be the new subsystem this issue's PRD
# says not to add.
_repo_slug() {
    local url
    url="$(git -C "$1" remote get-url origin 2>/dev/null)"
    if [ -z "$url" ]; then
        local first_remote
        first_remote="$(git -C "$1" remote 2>/dev/null | head -1)"
        [ -n "$first_remote" ] && url="$(git -C "$1" remote get-url "$first_remote" 2>/dev/null)"
    fi
    printf '%s' "$url" | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##'
}
KIT_REPO_SLUG="${KIT_REPO_SLUG:-$(_repo_slug "$KIT_DIR")}"
# Can't even tell what repo this is -- AC3-class "unknown answer", say nothing.
[ -z "$KIT_REPO_SLUG" ] && exit 0

DEPLOYED_SHA=""
[ -f "$DEPLOY_SHA_FILE" ] && DEPLOYED_SHA="$(cat "$DEPLOY_SHA_FILE" 2>/dev/null | tr -d '[:space:]')"
# AC3: absent, empty, or not shaped like a SHA at all -- silent, never a guess.
case "$DEPLOYED_SHA" in
    ""|unknown) exit 0 ;;
    *[!0-9a-fA-F]*) exit 0 ;;
esac

# AC4: bounded by `timeout`, and any failure (unreachable, unauthenticated, rate-limited)
# collapses to an empty string here, which the checks below treat as "say nothing".
MAIN_SHA="$(timeout 10s gh api "repos/$KIT_REPO_SLUG/commits/main" --jq .sha 2>/dev/null || echo "")"
[ -z "$MAIN_SHA" ] && exit 0

# AC2: current -- silent, so the line only ever appears when it means something.
[ "$DEPLOYED_SHA" = "$MAIN_SHA" ] && exit 0

# ahead_by counts commits MAIN has that DEPLOYED_SHA lacks -- exactly "N commits behind" from
# the deployed snapshot's point of view. A DEPLOYED_SHA this remote has never heard of (shallow
# clone, unfetched commit, stray local rebuild) 404s here just as an unreachable remote does --
# both collapse to the same silent AC3/AC4 outcome, which is correct: neither case can prove
# staleness, so neither should claim it.
AHEAD_BY="$(timeout 10s gh api "repos/$KIT_REPO_SLUG/compare/$DEPLOYED_SHA...$MAIN_SHA" --jq '.ahead_by' 2>/dev/null || echo "")"
case "$AHEAD_BY" in ''|*[!0-9]*) exit 0 ;; esac
[ "$AHEAD_BY" -eq 0 ] && exit 0

BUILD_DATE="$(timeout 10s gh api "repos/$KIT_REPO_SLUG/commits/$DEPLOYED_SHA" --jq .commit.committer.date 2>/dev/null || echo "")"
BUILD_TIME_HUMAN="an unknown time"
[ -n "$BUILD_DATE" ] && BUILD_TIME_HUMAN="$(date -u -d "$BUILD_DATE" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "$BUILD_DATE")"

SHORT_SHA="${DEPLOYED_SHA:0:7}"
echo "kit snapshot $SHORT_SHA is $AHEAD_BY commit(s) behind origin/main (built $BUILD_TIME_HUMAN) -- scripts under the kit path are NOT current main; verify fixes from a fresh worktree instead."
exit 0
