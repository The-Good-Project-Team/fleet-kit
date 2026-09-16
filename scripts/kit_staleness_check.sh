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
# the unreachable-remote branch below is silent by design (AC4), not an oversight.
#
# fk#1006: the UNTRACEABLE case is no longer silent. AC3 originally lumped three things
# together -- absent, `unknown`, and malformed `.deploy_sha` -- on the reasoning that none of
# them can PROVE the snapshot is stale, so none should claim it. That reasoning is right about
# staleness and wrong about what it leaves unsaid. An unreachable remote is transient: the next
# pass, an hour later, checks again and speaks. A `.deploy_sha` of `unknown` is permanent: the
# image was built without recording what it was built from, so this check can never say anything
# about that image, ever, and no pass is told that the answer is missing rather than reassuring.
# Measured live on the philanthropy instance 2026-09-14: `/fleet-kit/.deploy_sha` read `unknown`
# while the baked snapshot was two days behind main (`up.sh` had none of fk#1002, merged that
# morning), and every member pass in that window read silence from this script. Untraceable
# provenance is itself a proven fact, so it is reported as one -- without claiming staleness,
# which is still never guessed.
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
# fk#1006: absent, empty, `unknown`, or not shaped like a SHA -- the snapshot's provenance is
# untraceable. Still never a guess about staleness; it reports only what is proven, that this
# check cannot run against this image at all.
untraceable() {
    echo "kit snapshot provenance is untraceable ($DEPLOY_SHA_FILE $1) -- staleness of scripts under the kit path CANNOT be checked; verify fixes from a fresh worktree, not from $KIT_DIR."
    exit 0
}
case "$DEPLOYED_SHA" in
    "") [ -f "$DEPLOY_SHA_FILE" ] && untraceable "is empty" || untraceable "is missing" ;;
    unknown) untraceable "reads 'unknown' -- the image was built without recording its source commit" ;;
    *[!0-9a-fA-F]*) untraceable "is not a commit SHA" ;;
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
