#!/bin/bash
# tunnel_health_check.sh -- self-heal + page when the public tunnel URL is not actually
# serving the fleet view, independent of whether the container/process looks healthy locally.
#
# WHY THIS EXISTS: account_health_check.sh watches the account pool (LLM calls failing).
# Neither that script nor anything else here watches whether the PUBLIC url actually
# resolves to a live backend. Failure modes it catches: cloudflared tunnel ingress pointing
# at a stale/wrong local port (the confirmed case below), or the origin process bound to a
# different port than the tunnel expects. All of these leave the container "Up" and
# fleet_view answering fine on localhost while the public URL 502s.
#
# CONFIRMED LIVE 2026-08-25: cloudflared ingress pointed at localhost:8561 while
# fleet_view_server was bound to :8420 (FLEET_VIEW_PORT). Public URL 502'd; nobody paged
# until a human happened to load the page. Fixed once manually via the Cloudflare API
# (never via dashboard, per standing instruction -- see CF_API_TOKEN_FILE below). This
# script closes the gap for good: on the next drift, it corrects the ingress rule itself
# and pages what it did, no manual step required.
#
# WHAT IT WATCHES: curls PUBLIC_URL, checks for HTTP 200. On failure, reads the tunnel's
# ingress config via the Cloudflare API, finds the rule for PUBLIC_URL's hostname that has
# no `path` (the catch-all/root rule -- the one fleet_view_server owns), and if its port
# doesn't match FLEET_VIEW_PORT, rewrites just that rule and PUTs the full config back
# (Cloudflare's config PUT replaces the whole ingress array, so every other rule --
# /ssh, /webhook, other hostnames -- is round-tripped unchanged). Then re-checks PUBLIC_URL.
#
# WHY PLAIN BASH, HOST CRON, ZERO LLM: same reasoning account_health_check.sh and
# auto_deploy.sh already document -- the watcher can't depend on the thing being watched,
# and an LLM pass to fix "the LLM fleet's own view is unreachable" would be diagnosing
# itself with the tool that might be broken.
#
# CF API TOKEN: needs Account > Cloudflare Tunnel > Edit scope (read-only "Cloudflare
# Tunnel Read" is not enough -- this script writes the config on drift). Stored at
# /etc/cloudflared/api_token (600, root), account id at /etc/cloudflared/account_id.
# Tunnel ID is decoded from the connector token itself (/etc/cloudflared/token, base64
# JSON with fields a=account_id, t=tunnel_id, s=secret) so there's one less thing to keep
# in sync by hand.
#
# STATE: one marker file remembers whether we already paged for the CURRENT outage, so a
# 5-minute cron doesn't re-page every tick -- one page per outage, one recovery page after.
#
# Usage (cron, mirrors account_health_check.sh's own invocation shape):
#   */5 * * * * PUBLIC_URL=https://dino.luckymachines.co/ FLEET_VIEW_PORT=8420 \
#     NTFY_TOPIC=<topic> bash scripts/tunnel_health_check.sh >> .../tunnel_health_check.cron.log 2>&1
set -uo pipefail

PUBLIC_URL="${PUBLIC_URL:?set PUBLIC_URL -- the public URL to check, e.g. https://dino.luckymachines.co/}"
NTFY_TOPIC="${NTFY_TOPIC:-}"   # optional: fleet_alert.sh emails regardless
FLEET_VIEW_PORT="${FLEET_VIEW_PORT:-8420}"
STATE_FILE="${TUNNEL_HEALTH_STATE_FILE:-/home/ubuntu/fleet-kit-logs/.tunnel_health_paged.state}"
CONNECTOR_TOKEN_FILE="${CONNECTOR_TOKEN_FILE:-/etc/cloudflared/token}"
CF_API_TOKEN_FILE="${CF_API_TOKEN_FILE:-/etc/cloudflared/api_token}"
CF_ACCOUNT_ID_FILE="${CF_ACCOUNT_ID_FILE:-/etc/cloudflared/account_id}"

already_paged=""
[ -f "$STATE_FILE" ] && already_paged=$(cat "$STATE_FILE")

_ntfy() {
  local title="$1" msg="$2" priority="$3"
  bash /home/ubuntu/fleet-kit/scripts/fleet_alert.sh "fleet tunnel down" "$msg" \
    || echo "[alert] fleet_alert.sh failed" >&2
}

_check() {
  curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$PUBLIC_URL"
}

http_code=$(_check)

if [ "$http_code" = "200" ]; then
  if [ -n "$already_paged" ]; then
    _ntfy "fleet-kit: tunnel recovered" \
      "$PUBLIC_URL is serving again (HTTP 200) after an outage flagged at $already_paged." \
      "default"
    rm -f "$STATE_FILE"
  fi
  echo "[tunnel_health_check] healthy -- $PUBLIC_URL returned 200"
  exit 0
fi

echo "[tunnel_health_check] $PUBLIC_URL returned $http_code, attempting self-heal"

# --- self-heal: fix the ingress rule if it's pointing at the wrong local port ---
heal_result="not attempted -- missing CF API credentials"
if [ -r "$CONNECTOR_TOKEN_FILE" ] && [ -r "$CF_API_TOKEN_FILE" ] && [ -r "$CF_ACCOUNT_ID_FILE" ]; then
  TUNNEL_ID=$(python3 -c "
import base64, json, sys
raw = open('$CONNECTOR_TOKEN_FILE').read().strip()
print(json.loads(base64.b64decode(raw))['t'])
" 2>/dev/null)
  CF_API_TOKEN=$(tr -d '\n' < "$CF_API_TOKEN_FILE")
  ACCOUNT_ID=$(tr -d '\n' < "$CF_ACCOUNT_ID_FILE")
  HOSTNAME=$(echo "$PUBLIC_URL" | sed -E 's#^https?://##; s#/.*##')

  if [ -n "$TUNNEL_ID" ]; then
    config_json=$(curl -s -X GET \
      "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
      -H "Authorization: Bearer $CF_API_TOKEN")

    new_config=$(python3 -c "
import json, sys
data = json.loads('''$config_json''')
if not data.get('success'):
    print('ERROR: ' + json.dumps(data.get('errors')))
    sys.exit(1)
cfg = data['result']['config']
hostname = '$HOSTNAME'
port = '$FLEET_VIEW_PORT'
fixed = False
old_service = None
for rule in cfg['ingress']:
    if rule.get('hostname') == hostname and 'path' not in rule:
        old_service = rule.get('service')
        want = f'http://localhost:{port}'
        if old_service != want:
            rule['service'] = want
            fixed = True
        break
if fixed:
    print(json.dumps({'config': cfg}))
else:
    print('NOCHANGE:' + str(old_service))
" 2>&1)

    if [[ "$new_config" == \{* ]]; then
      put_result=$(curl -s -X PUT \
        "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H "Content-Type: application/json" \
        --data "$new_config")
      if echo "$put_result" | grep -q '"success":true'; then
        heal_result="ingress rule corrected to point at :$FLEET_VIEW_PORT, waiting for it to take effect"
        sleep 8
        http_code=$(_check)
      else
        heal_result="PUT failed: $put_result"
      fi
    elif [[ "$new_config" == NOCHANGE:* ]]; then
      heal_result="ingress already points at the right port (${new_config#NOCHANGE:}) -- outage is not a port mismatch"
    else
      heal_result="could not read/parse ingress config: $new_config"
    fi
  else
    heal_result="could not decode tunnel ID from $CONNECTOR_TOKEN_FILE"
  fi
fi

echo "[tunnel_health_check] self-heal: $heal_result"

if [ "$http_code" = "200" ]; then
  _ntfy "fleet-kit: tunnel self-healed" \
    "$PUBLIC_URL was down, auto-fixed: $heal_result. Now returning 200." \
    "default"
  rm -f "$STATE_FILE"
  echo "[tunnel_health_check] self-healed -- $PUBLIC_URL now returns 200"
  exit 0
fi

if [ -z "$already_paged" ]; then
  paged_at="$(date -u '+%Y-%m-%d %H:%M UTC')"
  _ntfy "🚨 fleet-kit: public URL down, self-heal did not fix it" \
    "$PUBLIC_URL still returns HTTP ${http_code:-timeout} after self-heal attempt: $heal_result" \
    "urgent"
  echo "$paged_at" > "$STATE_FILE"
  echo "[tunnel_health_check] PAGED -- $PUBLIC_URL still $http_code after self-heal"
else
  echo "[tunnel_health_check] still down (HTTP ${http_code:-timeout}), already paged at $already_paged"
fi
