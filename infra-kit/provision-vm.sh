#!/usr/bin/env bash
# provision-vm.sh — a physical/cloud Mac or Linux host, one multipass VM inside it, podman +
# git + gh ready to go. First of infra-kit's three scripts -- see README.md for the order.
#
# Provenance: the exact shape run live against `dino` (2026-08-21): Ubuntu 22.04 LTS multipass
# VM, 7 CPU / 12.7GB RAM / 82GB disk (generous defaults, not hand-tuned -- adjust
# INFRA_VM_CPUS/MEM/DISK below if you know your workload). podman 3.4.4 via plain `apt install`,
# no custom repo needed on 22.04.
#
# Usage: run ON the host machine (or via ssh -- this script has no remote-exec of its own,
# wrap it in `ssh <host> 'bash -s' < provision-vm.sh` if that's your shape).
#   INFRA_NAME=myworker ./provision-vm.sh
set -euo pipefail

NAME="${INFRA_NAME:?set INFRA_NAME -- becomes the VM name, e.g. 'myworker'}"
CPUS="${INFRA_VM_CPUS:-4}"
MEM="${INFRA_VM_MEM:-8G}"
DISK="${INFRA_VM_DISK:-40G}"
RELEASE="${INFRA_VM_RELEASE:-22.04}"

log() { printf '[provision-vm] %s\n' "$*"; }

# --- 1. multipass itself -----------------------------------------------------------------
if ! command -v multipass >/dev/null 2>&1; then
  log "multipass not found -- installing"
  if [[ "$(uname)" == "Darwin" ]]; then
    if command -v brew >/dev/null 2>&1; then
      brew install --cask multipass
    else
      log "FATAL: no brew and no multipass on macOS -- install manually: https://multipass.run"
      exit 1
    fi
  else
    log "FATAL: no automated multipass install path for $(uname) -- install manually: https://multipass.run"
    exit 1
  fi
else
  log "multipass already installed ($(multipass version | head -1))"
fi

# --- 2. the VM itself ----------------------------------------------------------------------
if multipass info "$NAME" >/dev/null 2>&1; then
  log "VM '$NAME' already exists -- skipping launch (state: $(multipass info "$NAME" --format csv | tail -1 | cut -d, -f2))"
  multipass start "$NAME" 2>/dev/null || true
else
  log "launching '$NAME' (${CPUS} CPU, ${MEM} mem, ${DISK} disk, Ubuntu ${RELEASE})"
  multipass launch "$RELEASE" --name "$NAME" --cpus "$CPUS" --memory "$MEM" --disk "$DISK"
fi

# Wait for the VM to actually be reachable before running anything inside it -- launch returns
# once the instance boots, not once cloud-init/networking is fully up.
log "waiting for '$NAME' to be ready for exec..."
for i in $(seq 1 30); do
  if multipass exec "$NAME" -- true 2>/dev/null; then
    break
  fi
  sleep 2
done

IP="$(multipass info "$NAME" --format csv | tail -1 | cut -d, -f3)"
log "VM ready at $IP"

# --- 3. podman + the tools fleet-kit (or anything else) will need inside ------------------
log "installing podman, git, gh, curl, jq inside '$NAME'..."
multipass exec "$NAME" -- bash -c '
  set -euo pipefail
  sudo apt-get update -qq
  sudo apt-get install -y -qq podman git curl jq
  if ! command -v gh >/dev/null 2>&1; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq gh
  fi
'
log "podman + git + gh installed"

log "done. Next: ./setup-tunnel.sh (needs INFRA_NAME=$NAME, INFRA_DOMAIN, CF_API_TOKEN, CF_ACCOUNT_ID)"
