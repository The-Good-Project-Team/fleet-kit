# infra-kit

Stand up a worker box -- a multipass VM on some host, podman inside it, a Cloudflare Tunnel
reaching it from the internet, and an ssh config that makes it a two-word command to reach.
Product-agnostic: fleet-kit is one thing you can point at this box once it exists, not the
other way around. If you need a container-capable box reachable from anywhere, with no port
forwarding and no static IP, this is that box.

Provenance: every piece here was run BY HAND once, live, against a real box (`dino`,
2026-08-21) while wiring fleet-kit's the-fixer webhook -- see that session for the actual
commands. This kit is those commands, generalized and idempotent, so the next box takes
minutes instead of a debugging session.

## What you get

```
                 internet
                     |
            Cloudflare Tunnel (cloudflared, systemd, always-on)
                     |
     +---------------+----------------+----------------+
     |  <name>.<your-domain>/ssh      /webhook         /  (or your own paths)
     v                v                v
  ssh://localhost:22  http://localhost:N  http://localhost:M   <- ingress rules,
                                                                    ONE tunnel, ONE hostname
     |
  the multipass VM ("<name>-vm"), inside "<name>-host" (the physical/cloud box running
  multipass) -- podman installed, ready for `fleet-kit/up.sh` or anything else that
  runs containers.
```

One hostname, N path-scoped ingress rules -- no subdomain sprawl. A dashboard, an SSH route,
and a webhook receiver all sit on the same `<name>.<your-domain>` behind different paths,
because Cloudflare Tunnel's ingress matching supports `path` for BOTH HTTP and TCP/SSH
services (this surprised us live -- see `setup-tunnel.sh`'s header for the exact API call that
proved it, when the docs read ambiguous).

## Naming: one word in, everything downstream matches

Set `INFRA_NAME=lucky` once and every layer below is named from it -- say "the lucky box" and
every artifact you'd go looking for (VM, ssh aliases, tunnel, keys) is `lucky` all the way
down. Only two things are separate axes on purpose: `INFRA_DOMAIN` (your own choice of public
hostname -- convention is `<name>.<your-domain>`, not enforced) and the podman CONTAINER name
inside the box, which is fleet-kit's own `up.sh --name`, keyed to the *target repo* you're
running, not the box -- one box can run several containers for several projects side by side.

| Layer | Pattern | Example (`INFRA_NAME=lucky`) | Set by |
|---|---|---|---|
| multipass VM | `$INFRA_NAME` | `lucky` | `provision-vm.sh` |
| host machine, ssh alias | `$INFRA_NAME-host` | `lucky-host` | `setup-ssh.sh` |
| VM over the host's LAN, ssh alias | `$INFRA_NAME-vm` | `lucky-vm` | `setup-ssh.sh` |
| VM over the internet (tunnel), ssh alias | `$INFRA_NAME` (bare -- same string as the VM itself) | `lucky` | `setup-ssh.sh` |
| Cloudflare Tunnel object | `$INFRA_NAME` | `lucky` | `setup-tunnel.sh` |
| public hostname | `$INFRA_DOMAIN` (free-standing, your call) | `lucky.example.com` | you set it |
| ssh key files | `$INFRA_NAME-<role>_ed25519` | `lucky-host_ed25519`, `lucky-vm_ed25519` | `setup-ssh.sh` |
| known_hosts file | `$INFRA_NAME-vm_known_hosts` | `lucky-vm_known_hosts` | `setup-ssh.sh` |
| ingress paths (on the one hostname) | your choice per service | `/ssh`, `/webhook`, `/` | `setup-tunnel.sh`'s `INFRA_INGRESS_JSON` |
| podman container *(fleet-kit's own `up.sh`, not this kit)* | `fleet-kit-<target-repo-name>` | `fleet-kit-nonprofit-atlas` | `up.sh --name` -- keyed to the PROJECT, not the box |
| podman image *(same)* | fixed tag | `fleet-kit:latest` | one image, many containers |

`ssh lucky-host` (direct), `ssh lucky-vm` (via the host's LAN), `ssh lucky` (via the internet) --
three ways to the same box, one word decides all three names.

## The four pieces

| Script | Does | Idempotent? |
|---|---|---|
| `provision-vm.sh` | Installs multipass (if needed) on the host, launches the VM, installs podman + `gh` + `git` inside it | Yes -- skips what already exists |
| `setup-tunnel.sh` | Creates a Cloudflare Tunnel (or reuses one), installs `cloudflared` in the VM as a systemd service, wires N path-scoped ingress rules via the API | Yes -- PUTs the whole ingress config, safe to re-run |
| `setup-ssh.sh` | Generates two ed25519 keypairs (host, VM), writes an ssh config block (`<name>-host`, `<name>-vm` LAN path, `<name>` tunnel path), installs the VM's pubkey | Yes -- won't overwrite existing keys |
| `README.md` (this file) | The recipe, and the mental model | -- |

Run in that order. Each is a standalone bash script, stdlib tools only (`ssh`, `curl`, `jq`) --
same "whatever runs this needs it already" reasoning as the rest of fleet-kit.

## An agent driving this install? Read AGENT_INSTALL.md first

The quickstart below is written for a human reading docs and running commands by hand.
[`AGENT_INSTALL.md`](AGENT_INSTALL.md) is the same install as a literal turn-by-turn: when to
stop and ask the human for a credential, exactly what to tell them to go create, where the
answer is allowed to live, and a verify step after the one that most commonly fails silently
wrong (an under-scoped Cloudflare token). Follow that file if you were asked to "set up
infra-kit" or "add water" to a new box; read on below for the human-facing version of the
same steps.

## Quickstart

```bash
cd infra-kit
export INFRA_NAME=myworker                    # becomes the VM name + ssh Host prefix
export INFRA_DOMAIN=myworker.example.com       # your own Cloudflare-managed domain
export CF_API_TOKEN=...                        # Zero Trust: Cloudflare Tunnel edit scope
export CF_ACCOUNT_ID=...                       # `wrangler whoami` prints this

./provision-vm.sh      # ~5 min: multipass launch + podman install
./setup-tunnel.sh       # ~1 min: tunnel create + DNS + ingress rules
./setup-ssh.sh          # ~10s: keys + ssh config block (prints what to append)
```

Then, from anywhere with `cloudflared` installed locally:

```bash
ssh -F ~/.ssh/myworker-config myworker   # reaches the VM over the internet, no port-forward
```

## What this does NOT do (bring your own)

- **DNS zone setup.** `INFRA_DOMAIN` must already be a hostname under a zone Cloudflare
  manages for your account -- this kit adds the ONE CNAME record for the tunnel, it doesn't
  register a domain or configure a zone.
- **Cloudflare Access.** The `/ssh` and any other sensitive path get zero access-control layer
  beyond whatever auth the service itself provides (SSH key auth, in the default `/ssh` route)
  unless you separately enable Access in the Cloudflare dashboard and wrap a path in a policy.
  `setup-tunnel.sh` prints a reminder; enabling Access itself needs one dashboard click this
  kit's API calls can't make on your behalf (Cloudflare's own API returns
  `access.api.error.not_enabled` until a human flips it on once, account-wide).
- **What runs inside the VM.** podman is installed and ready; `fleet-kit/up.sh` (or your own
  `podman run`) is the next step, not part of this kit.

## Verification status, honestly

`provision-vm.sh` and the ingress-rule half of `setup-tunnel.sh` are the exact commands run
live against a real box (`dino`, 2026-08-21) -- multipass launch, podman install, the
path-scoped ingress PUT, all proven working end to end (see the fleet-kit session that
produced this kit for the pasted output). Tunnel CREATE-from-scratch and the DNS CNAME step in
`setup-tunnel.sh` were written from Cloudflare's documented API shape but NOT exercised live
in that session (the only tunnel touched already existed) -- run them once on a throwaway
tunnel name before trusting them on anything real, same as you'd trust any new script.

## Provenance notes on the two non-obvious things

**Path-scoped ingress works for non-HTTP services.** The Cloudflare docs read as if `path` only
applies to HTTP-family services. Tested live: a `ssh://localhost:22` ingress rule with
`"path": "/ssh"` alongside an `http://localhost:8561` rule with no path, both on the SAME
hostname, both accepted by the API and both worked end to end (`cloudflared access ssh
--hostname <host>/ssh` connected clean). This is why `setup-tunnel.sh` defaults to one hostname,
many paths, rather than provisioning a subdomain per service.

**GitHub webhooks sometimes deliver form-urlencoded even when configured as JSON.** Unrelated
to this kit directly, but if you're wiring a webhook receiver behind this tunnel: GitHub's
`config.content_type` can read "application/json" via the API while a real delivery arrives
`Content-Type: application/x-www-form-urlencoded` anyway. Unwrap the `payload=` field
defensively rather than trusting the configured value matches the wire format.
