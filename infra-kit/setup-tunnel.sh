#!/usr/bin/env bash
# setup-tunnel.sh — a Cloudflare Tunnel reaching the VM from the internet, with N
# path-scoped ingress rules on ONE hostname. Second of infra-kit's three scripts.
#
# Provenance: every API call here is the exact shape run live against a real account
# (2026-08-21), generalized. Raw `curl` against api.cloudflare.com throughout -- not the
# `wrangler tunnel` CLI (its `create` subcommand works but is marked [experimental] and has no
# documented non-interactive auth path; a plain API token is the portable, scriptable one).
#
# THE ONE NON-OBVIOUS THING THIS PROVES: Cloudflare's ingress `path` field works for non-HTTP
# services too. Tested live: an `ssh://localhost:22` rule with `"path": "/ssh"` sitting
# alongside an `http://...` rule with no path, both on the SAME hostname -- both matched
# correctly, `cloudflared access ssh --hostname <host>/ssh` connected clean. This is why
# INFRA_PATHS below defaults to path-per-service on one hostname instead of a subdomain per
# service.
#
# Usage:
#   INFRA_NAME=myworker INFRA_DOMAIN=myworker.example.com \
#   CF_API_TOKEN=... CF_ACCOUNT_ID=... ./setup-tunnel.sh
#
# CF_API_TOKEN needs: Account.Cloudflare Tunnel:Edit, Zone.DNS:Edit (for the CNAME).
# Mint one at https://dash.cloudflare.com/profile/api-tokens -- "Edit Cloudflare Tunnel"
# template covers the first; add the DNS zone permission for your zone to the same token.
set -euo pipefail

NAME="${INFRA_NAME:?set INFRA_NAME (same value used in provision-vm.sh)}"
DOMAIN="${INFRA_DOMAIN:?set INFRA_DOMAIN -- e.g. myworker.example.com, must be under a zone your account manages}"
TOKEN="${CF_API_TOKEN:?set CF_API_TOKEN -- Account.Cloudflare Tunnel:Edit + Zone.DNS:Edit}"
ACCT="${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID -- find it: curl -s -H \"Authorization: Bearer \$CF_API_TOKEN\" https://api.cloudflare.com/client/v4/accounts | jq}"
API="https://api.cloudflare.com/client/v4"

log() { printf '[setup-tunnel] %s\n' "$*"; }
cf() { curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }

# --- 1. find or create the tunnel -----------------------------------------------------------
log "checking for an existing tunnel named '$NAME'..."
EXISTING=$(cf "$API/accounts/$ACCT/cfd_tunnel?name=$NAME&is_deleted=false" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d.get('result') or []
print(r[0]['id'] if r else '')
")

if [ -n "$EXISTING" ]; then
  TUNNEL_ID="$EXISTING"
  log "reusing existing tunnel '$NAME' ($TUNNEL_ID)"
  TOKEN_SECRET=""  # can't re-fetch a token for an existing tunnel via this endpoint; see step 3
else
  log "creating tunnel '$NAME'..."
  CREATE_OUT=$(cf -X POST "$API/accounts/$ACCT/cfd_tunnel" \
    -d "{\"name\": \"$NAME\", \"config_src\": \"cloudflare\"}")
  TUNNEL_ID=$(echo "$CREATE_OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['id'])")
  log "created tunnel '$NAME' ($TUNNEL_ID)"
fi

# --- 2. get a connector token (only obtainable at creation OR via this dedicated endpoint) ---
TUNNEL_TOKEN=$(cf "$API/accounts/$ACCT/cfd_tunnel/$TUNNEL_ID/token" | python3 -c "import json,sys; print(json.load(sys.stdin)['result'])")
if [ -z "$TUNNEL_TOKEN" ] || [ "$TUNNEL_TOKEN" = "None" ]; then
  log "FATAL: could not obtain a connector token for tunnel $TUNNEL_ID -- check CF_API_TOKEN's Tunnel:Edit scope"
  exit 1
fi

# --- 3. install cloudflared inside the VM as a systemd service ------------------------------
log "installing cloudflared inside '$NAME'..."
multipass exec "$NAME" -- bash -c "
  set -euo pipefail
  if ! command -v cloudflared >/dev/null 2>&1; then
    sudo mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared jammy main' \
      | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq cloudflared
  fi
  sudo mkdir -p /etc/cloudflared
  echo '$TUNNEL_TOKEN' | sudo tee /etc/cloudflared/token >/dev/null
  sudo chmod 600 /etc/cloudflared/token
  sudo cloudflared service install \"\$(cat /etc/cloudflared/token)\" 2>&1 | grep -v 'already installed' || true
  sudo systemctl enable cloudflared 2>/dev/null || true
  sudo systemctl restart cloudflared
"
log "cloudflared installed + running"

# --- 4. DNS: one CNAME for the whole hostname -------------------------------------------------
ZONE_NAME=$(echo "$DOMAIN" | awk -F. '{print $(NF-1)"."$NF}')
log "resolving zone for '$ZONE_NAME'..."
ZONE_ID=$(cf "$API/zones?name=$ZONE_NAME" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d.get('result') or []
print(r[0]['id'] if r else '')
")
if [ -z "$ZONE_ID" ]; then
  log "FATAL: no zone found for '$ZONE_NAME' -- is it managed by this Cloudflare account?"
  exit 1
fi

log "pointing '$DOMAIN' -> tunnel via CNAME..."
cf -X POST "$API/zones/$ZONE_ID/dns_records" \
  -d "{\"type\":\"CNAME\",\"name\":\"$DOMAIN\",\"content\":\"$TUNNEL_ID.cfargotunnel.com\",\"proxied\":true}" \
  > /dev/null || log "  (CNAME may already exist -- not fatal, continuing)"

# --- 5. ingress rules: one hostname, path-scoped services -----------------------------------
# Default set below is a starting point, not gospel -- edit to match what you're actually
# running. Each rule needs {hostname, [path], service}; the LAST rule (no hostname/path) is
# the catch-all and must stay http_status:404 or every unmatched path 200s into nothing.
log "wiring ingress rules on '$DOMAIN'..."
INGRESS_JSON="${INFRA_INGRESS_JSON:-$(cat <<EOF
[
  {"hostname": "$DOMAIN", "path": "/ssh", "service": "ssh://localhost:22"},
  {"hostname": "$DOMAIN", "service": "http://localhost:8561"},
  {"service": "http_status:404"}
]
EOF
)}"
cf -X PUT "$API/accounts/$ACCT/cfd_tunnel/$TUNNEL_ID/configurations" \
  -d "{\"config\": {\"ingress\": $INGRESS_JSON}}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('  ingress applied:' if d.get('success') else '  FAILED:', d.get('errors') or [r['hostname']+r.get('path','') for r in d['result']['config']['ingress'] if 'hostname' in r])
"

log "done. Test: cloudflared access ssh --hostname $DOMAIN/ssh (once setup-ssh.sh wires the local ssh config)"
log ""
log "NOTE: Cloudflare Access is likely NOT enabled on a fresh account -- every path here is"
log "reachable by anyone who has the URL + whatever the SERVICE's own auth requires (an ssh"
log "key, an HMAC secret you check yourself, etc). Enable Access in the dashboard"
log "(https://dash.cloudflare.com -> Zero Trust -> click 'Enable') and wrap a sensitive path"
log "in a policy if that's not enough for your case -- the API returns"
log "'access.api.error.not_enabled' until a human clicks that button once, account-wide."
