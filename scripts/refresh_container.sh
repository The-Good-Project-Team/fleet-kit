#!/bin/bash
# refresh_container.sh — restart a fleet-kit container to pick up a host-side fleet.env edit,
# without the rootlessport port-bind race a bare `podman restart` can hit.
#
# WHY THIS EXISTS: a `:ro` bind mount can hold a stale inode after the host edits the mounted
# file in place (sed -i, most editors -- anything that replaces-then-renames rather than
# truncate-and-rewrite). The container keeps serving the OLD content until its mount is torn
# down and recreated. `podman restart` does that, but on this box (rootless podman, published
# ports) it can race its own port cleanup: `podman restart` tears down the old process before
# the kernel has released the port, so the new one's `podman run`-equivalent bind fails with
# "address already in use" (rootlessport listen tcp ...). `podman start` on the now-dead
# container retries clean. Found live on dino, 2026-08-21/22, twice.
#
# Usage: refresh_container.sh <container-name>
set -euo pipefail
NAME="${1:?usage: refresh_container.sh <container-name>}"

if ! podman restart "$NAME" 2>/tmp/refresh_container.err; then
  if grep -q "address already in use" /tmp/refresh_container.err; then
    echo "[refresh] restart hit the rootlessport race, retrying via start..."
    sleep 1
    podman start "$NAME"
  else
    cat /tmp/refresh_container.err >&2
    rm -f /tmp/refresh_container.err
    exit 1
  fi
fi
rm -f /tmp/refresh_container.err
echo "[refresh] $NAME is up:"
podman ps --filter "name=$NAME" --format "{{.Names}}\t{{.Status}}"
