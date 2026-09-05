#!/bin/bash
# publish_share.sh -- write THIS instance's own FLEET_SHARE_FRACTION into the shared,
# cross-instance FLEET_SHARE_DIR so a sibling container's check_share_sum.sh can see it
# without either container gaining filesystem access to the other's fleet.env (gh#293).
#
# check_share_sum.sh cannot see sibling instances from inside a container: the bind-mount
# design (entrypoint.sh/deploy.sh) only ever gives one container its OWN fleet.env, never the
# `instances/` parent tree those fractions used to be scanned from (that tree also carries
# every instance's live CLAUDE_CODE_OAUTH_TOKEN/FLEET_MAXX_KEY, so mounting it wholesale would
# leak credentials across instances). FLEET_SHARE_DIR is a small, purpose-built shared
# directory -- same shape as FLEET_LEASE_DIR/maxx_lease.py's already-shipped fix for the
# identical cross-instance-visibility problem -- that carries ONLY {instance, fraction,
# published_at} per file, nothing secret.
#
# Called from two places: entrypoint.sh at container boot (so a sibling has SOMETHING to read
# even before this instance's first jefe pass), and check_share_sum.sh itself at the start of
# every run (so a hand-edited FLEET_SHARE_FRACTION in fleet.env -- no redeploy -- is reflected
# on this instance's very next check, same "re-source before every use" discipline
# entrypoint.sh's own crontab lines already follow). A no-op when FLEET_SHARE_DIR is unset --
# host-side runs and pre-fix deployments are unaffected.
set -uo pipefail

[ -n "${FLEET_SHARE_DIR:-}" ] || exit 0
mkdir -p "$FLEET_SHARE_DIR" 2>/dev/null || exit 0

name="${FLEET_INSTANCE_NAME:-default}"
# Unset means "no cap" -- the whole pot, the same honest-read default check_share_sum.sh's own
# scan() has always used for a fleet.env with no FLEET_SHARE_FRACTION line.
frac="${FLEET_SHARE_FRACTION:-1.0}"
tmp="$FLEET_SHARE_DIR/.${name}.json.tmp.$$"
printf '{"instance":"%s","fraction":%s,"published_at":%s}\n' "$name" "$frac" "$(date +%s)" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$FLEET_SHARE_DIR/${name}.json" 2>/dev/null
