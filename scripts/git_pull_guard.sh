#!/bin/bash
# git_pull_guard.sh -- self-healing wrapper around the gitpull cron's `git pull --ff-only`.
#
# gh#68 (originally nonprofit-atlas#3130, recurred 3x): a bare `git pull --ff-only` fails hard,
# and stays failed, whenever the checked-out branch's history can never fast-forward onto
# origin/main again -- the common case being a squash-merged PR whose branch got deleted
# upstream (content lands on main under a brand-new SHA, the old branch tip can never become an
# ancestor of it). Left alone, that spins entrypoint.sh's */10 cron on the same dead ref forever
# until a human/roomba notices and manually runs `git checkout main && git pull`. This is that
# recovery, automated.
#
# Fetches `origin main` directly rather than relying on the checked-out branch's own configured
# upstream -- so a branch whose tracked remote ref was deleted entirely never even reaches a
# "no such ref was fetched" error; it just fails the ancestor check below and self-heals.
#
# Usage: git_pull_guard.sh [repo_dir]   (defaults to cwd, matching entrypoint.sh's `cd
# $FLEET_REPO && git_pull_guard.sh` call site)
set -uo pipefail
# Deliberately not `set -e`: every git call's exit status is inspected explicitly below so
# failures can be routed into the self-heal path instead of aborting the script outright.

REPO_DIR="${1:-$(pwd)}"
cd "$REPO_DIR" || { echo "git_pull_guard: cannot cd to $REPO_DIR"; exit 1; }

if [ ! -d .git ]; then
    echo "git_pull_guard: $REPO_DIR is not a git checkout -- refusing to guess, leaving it alone"
    exit 1
fi

# gh#255: this cron's own tick and a host-side script touching the exact same .git directory
# (auto_deploy.sh, when this box self-hosts fleet-kit and $FLEET_REPO's bind-mount happens to be
# that same checkout) raced on refs/remotes/origin/main with no lock between them at all.
# Locking on the .git dir itself, rather than a fleet-kit-specific path, means this serializes
# against anything else touching THIS checkout automatically when they really are the same
# directory, and costs nothing (an uncontended, instantly-released lock) when they aren't.
LOCKFILE="$(pwd)/.git/fleet_pull.lock"
exec 8>"$LOCKFILE"
if command -v flock >/dev/null 2>&1; then
    flock 8
fi

if ! git fetch origin main -q; then
    echo "git_pull_guard: fetch of origin main failed -- network/auth issue, not a stray-branch problem. Leaving checkout untouched."
    exit 1
fi

LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
REMOTE_SHA="$(git rev-parse origin/main)"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    echo "git_pull_guard: already up to date at $REMOTE_SHA"
    exit 0
fi

if [ -n "$LOCAL_SHA" ] && git merge-base --is-ancestor "$LOCAL_SHA" "$REMOTE_SHA" 2>/dev/null; then
    if git merge --ff-only origin/main -q; then
        echo "git_pull_guard: fast-forwarded $LOCAL_SHA -> $(git rev-parse HEAD)"
        exit 0
    fi
    echo "git_pull_guard: fast-forward merge failed unexpectedly after passing the ancestor check -- leaving checkout as-is for a human to inspect"
    exit 1
fi

# Not an ancestor: the checked-out branch's history can never reach origin/main by fast-forward
# (deleted/stray branch after a squash-merge, most commonly). Self-heal onto main rather than
# leave the next 9 ticks this hour hitting the identical dead end.
echo "git_pull_guard: SELF-HEAL: HEAD ($LOCAL_SHA) is not an ancestor of origin/main ($REMOTE_SHA) -- checking out main"
if git checkout -B main origin/main -q; then
    echo "git_pull_guard: SELF-HEAL: recovered onto main at $(git rev-parse HEAD)"
    exit 0
fi
echo "git_pull_guard: SELF-HEAL FAILED -- checkout -B main origin/main errored, checkout left as-is for a human to inspect"
exit 1
