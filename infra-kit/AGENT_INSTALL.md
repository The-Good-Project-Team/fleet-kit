# Install checklist — for the agent driving this, not the human reading it

Everything else in this kit (README.md, the 3 scripts) documents WHAT each piece does. This
file is the literal turn-by-turn an agent follows to actually run the install end to end,
including exactly when to stop and ask the human for something, and exactly where the answer
goes. Read this file first if you (an agent) were just asked to "set up infra-kit" or "add
water" to a new box.

**Rule for every credential below: ask the human to run the auth/lookup command themselves in
their own terminal and paste back only the value you need — never ask them to paste a raw
token into chat if a `read -s` prompt or their own clipboard avoids it, and never store a
credential anywhere but the exact file named below.** These are real API tokens; treat them
with the same care as any other secret this repo touches.

## Step 0 — one name, decide it now

Ask: **"What should this box be called?"** (single word, becomes the VM name and every ssh
alias — see README's naming table). Set once:

```bash
export INFRA_NAME=<their-answer>       # e.g. "lucky"
```

Ask: **"What public hostname should reach it?"** — must be a hostname under a domain/zone
they already manage in Cloudflare (this kit adds one CNAME, it does not register a domain).

```bash
export INFRA_DOMAIN=<their-answer>     # e.g. "lucky.example.com"
```

## Step 1 — provision the VM (no credentials needed yet)

```bash
./provision-vm.sh
```

Nothing to ask for here. If it fails, read its own error — it's local multipass/podman
install, not a credential problem.

## Step 2 — Cloudflare API token (STOP, this is the first real credential)

Say to the human, verbatim in spirit: **"Now I need a Cloudflare API token scoped to Tunnel
edit + DNS edit — not your account password, a scoped token you create once."**

Tell them exactly how to make it (do not create it for them — this is their Cloudflare
account):
1. https://dash.cloudflare.com/profile/api-tokens → **Create Token** → **Create Custom Token**
2. Permissions: **Account.Cloudflare Tunnel:Edit** + **Zone.DNS:Edit** (scoped to the zone
   `INFRA_DOMAIN` lives under — never "All zones" if a scoped choice is offered)
3. Copy the token once shown (Cloudflare will not show it again)

Ask them to paste the token value back, or `export CF_API_TOKEN=...` in their own shell if
they'd rather you never see it directly — either is fine, but never write it to a file
yourself outside the shell env unless the human explicitly asks you to persist it (e.g. into
a `.env` this repo already gitignores).

Then get the account ID (no new credential, just a lookup using the token they just gave you):

```bash
curl -s -H "Authorization: Bearer $CF_API_TOKEN" https://api.cloudflare.com/client/v4/accounts | jq
```

Take the `id` field from the result:

```bash
export CF_ACCOUNT_ID=<from the jq output above>
```

**Verify before moving on** (catches an under-scoped token immediately, with a clear error
instead of a mysterious later failure):

```bash
curl -s -H "Authorization: Bearer $CF_API_TOKEN" \
  https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/cfd_tunnel | jq '.success'
```

`true` → token is scoped correctly, proceed. `false` with `access.api.error.not_enabled` or
similar → the token's missing the Tunnel:Edit scope; send the human back to step 2 to fix the
token's permissions rather than guessing further.

## Step 3 — run the tunnel setup

```bash
./setup-tunnel.sh
```

This is the one step the kit's own README flags as "written from the documented API shape,
not exercised live end-to-end for a from-scratch tunnel" — if this is a genuinely new
`INFRA_NAME`/tunnel (not reusing one that already exists), tell the human that up front so an
unexpected error here isn't a surprise, and read its output for the FATAL check on Tunnel:Edit
scope specifically (line ~59 of the script) if it fails.

## Step 4 — ssh config

```bash
./setup-ssh.sh
```

No new credentials — this generates its own keypairs and prints an ssh config block. Ask the
human where they want it appended (their real `~/.ssh/config`, or a kit-specific include file)
— **do not append to their real ssh config file without asking first**, since that's an edit
to a file outside this repo they may already curate by hand.

## Step 5 — prove it end to end

```bash
ssh -F ~/.ssh/<their-config> $INFRA_NAME   # or whatever alias step 4 printed
```

If this connects, hand off to fleet-kit's own install checklist (its README's "Install"
section) for the next layer — infra-kit's job ends at "a box podman is ready on, reachable
from anywhere." Auth for `gh` and Claude accounts belongs to THAT checklist, not this one.

## What NOT to do, ever, in this flow

- Never generate or choose a Cloudflare API token on the human's behalf — it must come from
  their own dashboard, their own account.
- Never write a raw token to a file this repo will commit, or paste it back into chat/logs
  once received — treat it exactly like any other secret this session touches.
- Never silently retry a FATAL from `setup-tunnel.sh` with a broader token scope guess — stop
  and tell the human what scope is actually missing, let them fix the token themselves.
- Never skip the Step 2 verify curl "to save time" — an under-scoped token's real failure mode
  is a confusing later error (`access.api.error.not_enabled`) that reads like a different bug
  entirely if you haven't already ruled out the scope.
