# worktree_prune.sh -- container-aware worktree pruning (gh#684)
#
# `git worktree prune` is a blanket operation: it walks EVERY entry registered in $REPO's
# .git/worktrees admin dir and deletes any whose working-tree path this process can't see.
# $REPO (/repo) is a bind mount SHARED across containers, but a run worktree lives under
# ${TMPDIR:-/tmp} (run_member.sh's $WT_PATH), which is PER-CONTAINER. During a rolling cutover
# (#626) the new container's blanket prune therefore sees the OLD container's still-live
# worktree as "gone" and deletes its .git/worktrees entry out from under the still-running
# pass -- observed live 2026-09-08 03:06Z (minion-item4863 spent its first three minutes on
# `git worktree repair` + a full re-clone instead of building). See #684.
#
# Fix: stamp every worktree this container creates with its own identity, then prune only
# entries stamped with THIS container's identity. An entry with no stamp (created before this
# fix, or mid-creation) or one stamped for a different container is left registered untouched --
# it might be a live pass in another container and there is no reliable way to tell, so the safe
# default is to not touch it. This adds no new cross-container coordination or locking (#684
# non-goal): ownership is made legible via the stamp, not negotiated.

# worktree_container_id: an identifier stable for this container's whole lifetime that differs
# across a rolling cutover. Reuses the same signal up.sh's orphaned-entrypoint reaper already
# relies on for container identity: the container runtime sets HOSTNAME to the container's own
# short ID for every process inside it. Falls back to reading it off the filesystem if some
# caller has unset/overridden the env var. Empty output means "could not resolve" -- callers
# must treat that as "prune nothing", never as a wildcard match.
worktree_container_id() {
  if [ -n "${HOSTNAME:-}" ]; then
    printf '%s' "$HOSTNAME"
  elif [ -r /etc/hostname ]; then
    tr -d '[:space:]' < /etc/hostname
  else
    printf '%s' ""
  fi
}

# worktree_stamp_container_id <repo> <worktree_path>: record which container created the
# worktree already registered at <worktree_path> via `git worktree add`. Callers must run this
# BEFORE releasing worktree_lock.sh's lock on the creating call, so a concurrent prune from any
# container never observes the entry half-stamped.
worktree_stamp_container_id() {
  local repo="$1" wt_path="$2" cid d gd
  cid="$(worktree_container_id)"
  [ -n "$cid" ] || return 0
  for d in "$repo"/.git/worktrees/*/; do
    [ -f "${d}gitdir" ] || continue
    gd="$(cat "${d}gitdir" 2>/dev/null)"
    if [ "${gd%/.git}" = "$wt_path" ]; then
      printf '%s' "$cid" > "${d}container-id"
      return 0
    fi
  done
  return 1
}

# worktree_prune_own_container <repo>: replaces a blanket `git worktree prune`. Reclaims a
# stale entry ONLY when both (a) its working-tree path no longer exists and (b) it is stamped
# with THIS container's own identity. Locked entries are skipped exactly as git's own prune
# would skip them.
worktree_prune_own_container() {
  local repo="$1" cid d gd wt_path entry_cid
  cid="$(worktree_container_id)"
  [ -d "$repo/.git/worktrees" ] || return 0
  for d in "$repo"/.git/worktrees/*/; do
    [ -d "$d" ] || continue
    [ -f "${d}locked" ] && continue
    [ -f "${d}gitdir" ] || continue
    gd="$(cat "${d}gitdir" 2>/dev/null)"
    wt_path="${gd%/.git}"
    [ -e "$wt_path" ] && continue
    [ -n "$cid" ] || continue
    entry_cid="$(cat "${d}container-id" 2>/dev/null || true)"
    if [ "$entry_cid" = "$cid" ]; then
      rm -rf "$d"
    fi
  done
  return 0
}
