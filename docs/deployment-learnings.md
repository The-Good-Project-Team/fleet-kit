# Deployment learnings — first real box (2026-08-20/21, "dino")

Captured while wiring fleet-kit onto a fresh Ubuntu multipass VM against nonprofit-atlas,
retiring an existing 18-launchd-job fleet on the same physical host in the process. Every
trap below cost real back-and-forth; write them down once so the next box skips them.

## 1. Hand-provisioning a box is not reproducible — use the Dockerfile

The VM ("dino") had been provisioned by hand across multiple sessions over weeks: Podman
installed, then gone after a rebuild; Claude CLI installed, confirmed working, needed
reinstalling in this same session after appearing absent; `~/.config/lucky/` held env files
from THREE prior, unrelated projects (an old `lucky` API daemon, a retired magikarp
install, stray ssh configs pointing at a since-renamed host) — none of it documented
anywhere except chat transcripts. A builder run tripped over one of these (a stray SSH
process reaching for `lucky2@192.168.64.1`, a host alias that no longer means what the env
file thought it meant) and silently died with no error, just a frozen log.

**Fix implemented:** `Dockerfile` + `entrypoint.sh` at the repo root. Nothing box-specific is
baked in; credentials/token/repo are all runtime inputs (env vars + mounts). A dead box is
`docker run` on a new one, not an archaeology session reading old chat transcripts.

## 2. `curl | bash` install of the Claude CLI can silently half-succeed

`curl -fsSL https://claude.ai/install.sh | bash` intermittently failed with `curl: (23)
Failure writing output to destination` / `Download failed` when run through a chained
ssh -> multipass exec wrapper with a short timeout — but the binary and symlink had ALREADY
landed at `~/.local/bin/claude` by the time the "failure" was reported. The install script's
own post-install verification step was what hung/timed out, not the download.

**Lesson:** after ANY install script reports failure, check `which claude` (or the target
binary) directly before assuming the install didn't happen — a wrapped/piped install can
report failure on a step after the thing you actually needed already succeeded. The
Dockerfile avoids the whole class of problem: the install runs in a clean, un-chained `RUN`
step at build time, and a failed build simply doesn't produce an image.

## 3. `account_pool_run` (the distilled version) never actually switched credentials

The generic `scripts/account_pool.sh` shipped in this kit iterated `FLEET_ACCOUNTS` and
tried each name, but never set `CLAUDE_CONFIG_DIR` per account — every attempt ran under
whichever identity was already logged into the CLI's default config dir. Failover was
theater: a second account *name* in the pool changed nothing about which credential actually
ran. Fixed in PR #1 (this repo) with a mutation test (marker-file harness, RED without the
fix / GREEN with it — see the PR body for the exact commands). The convention going forward:
account `"foo"` in `FLEET_ACCOUNTS` maps to `$HOME/.claude-foo` — log each one in with
`CLAUDE_CONFIG_DIR="$HOME/.claude-foo" claude setup-token` before it's usable.

**Container implication:** each `$HOME/.claude-<account>` must be a mounted volume, never
baked into the image (credentials don't belong in a layer that might get pushed anywhere).
`entrypoint.sh` checks for these at boot and warns loudly if one is missing, rather than
letting the first cron tick fail silently three hours later.

## 4. `gh secret set` (GitHub Actions secrets) cannot be read back — wrong tool for a
   standalone daemon's credentials

GitHub Actions secrets are injected as env vars ONLY inside a workflow run on a GitHub
runner; `gh` itself has no read path for a secret's value (by design — GitHub never returns
one once written). A standalone box running a long-lived `claude -p` loop 24/7 is not a
workflow run, so "store the token in GH secrets, have the box pull it" does not work as a
delivery mechanism, though it's still worth doing as a durable, rotatable *record* of which
token is live. The actual runtime credential has to reach the box (or container mount) by a
different path — direct authenticated copy, a real secrets manager, or (for this kit's
default assumption) a bind-mounted `$HOME/.claude-<account>` directory populated once,
out of band, by a human.

## 5. Two independent fleets sharing one GitHub Issues board is a real collision, not a
   theoretical one

nonprofit-atlas already ran a full 18-job launchd fleet (magikarp, on the same physical host
as this box) using the identical `fleet:backlog` / `fleet:claimed` label convention this kit
uses by default. Before pointing a second claiming system at the same repo, check for an
existing board consumer — `gh issue list --label fleet:backlog` and grep launchd/systemd/cron
for anything already claiming from it. Running two claimers against one board without
coordination produces exactly the kind of double-claim bug nonprofit-atlas's own board was
independently reporting on itself at the time (issue: every open item stuck
`fleet:claimed` with nothing ever removing the label — a one-way ratchet from *some*
claimer's bug, not necessarily this kit's).

If migrating a repo from one fleet implementation to another, retire the old scheduler
first (`launchctl unload` / `systemctl disable`, don't delete — reversible) and confirm zero
processes running before pointing the new one at the same board.

## 6. `--permission-mode acceptEdits` still gates Bash execution — an unattended builder needs
   `--dangerously-skip-permissions`

First real end-to-end build attempt (issue #3047, nonprofit-atlas) wrote a correct fix and a
correct test — file reads and edits went through fine — then hard-blocked on every actual
command: `bash -n`, `python3`, `git add`, even `echo hello > file` all returned "This command
requires approval." A dispatched sub-persona hit the identical wall independently. The model
did the right thing: it did not fabricate a passing test result or a fake PR, it reported the
blocker plainly and stopped (see rule 1 of this kit's persona charter — paste real output,
never fabricate).

Root cause: `--permission-mode acceptEdits` only auto-approves `Write`/`Edit` tool calls. It
does NOT cover `Bash` — that stays gated behind an interactive approval prompt, and a
worktree with no human attached has no one to answer it. `worktree_builder.sh` and
`run_agent_pass.sh` both fixed to use `--dangerously-skip-permissions` instead: the fresh,
throwaway worktree IS the isolation boundary (per this kit's own `persona_law.md`), which is
what makes skipping the interactive prompt safe specifically here. `code_review_local.sh`
was left alone — it only reads and posts a status, no Bash execution needed.

**If you see a build session report a completed fix but no commit/PR, check for this exact
wall before assuming the model failed** — the fix may be sitting correct and untested in a
worktree that already got cleaned up by the script's own `cleanup()` trap.

## 7. `worktree_builder.sh`'s log goes silent on a hung subprocess — no heartbeat, no timeout
   surfaced to the log

When the builder's `claude -p` call hung on the stray-SSH issue above, `/tmp/builder_run.log`
simply stopped growing — no "still running", no partial-timeout warning, nothing
distinguishing "working on something slow" from "wedged". Worth hardening in a future pass:
periodic heartbeat lines while the build subprocess runs, and an explicit log line the moment
`$TIMEOUT_S` is hit rather than only the process disappearing.
