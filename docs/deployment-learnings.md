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

## 7. Claiming a board item has no release path on failure — a killed/failed build leaves it
   `fleet:claimed` forever

`board_github.py`'s contract (see its own module docstring) is claim -> add label, done ->
close issue. There is no `unclaim`/`release` call anywhere in `worktree_builder.sh` for the
failure path — if the build subprocess is killed, times out, or exits non-zero, the item
stays labeled `fleet:claimed` with no worker actually working it, permanently, until a human
notices and manually strips the label. Hit this directly: killed a stuck build session by
hand and the seed issue stayed claimed.

This is NOT a theoretical risk — the exact same defect, independently discovered against a
different (older, hand-rolled) fleet implementation on the same day, was nonprofit-atlas
issue #3044: "the board's claim label is a ONE-WAY RATCHET: 374 of 374 open backlog items are
fleet:claimed, no code path ever removes it." Two independent fleet implementations hit the
identical failure mode, which suggests it's inherent to "claim by adding a label" rather than
either implementation's bug specifically.

**Not yet fixed here** — needs a `board_github.py release <id> <worker>` verb (strip the
label, comment why) called from `worktree_builder.sh`'s failure branches (`RC -ne 0`, the
"no PR URL found" branch, and ideally a trap on unexpected exit/kill too). Left as an open
item for the next pass rather than rushed in without testing the failure-path plumbing
properly.

## 9. A copied credentials file with `expiresAt: 0` fails auth-refresh silently and
   `worktree_builder.sh` swallows the error entirely

Copying `~/.claude/.credentials.json` off macOS Keychain by piping through
`python3 -c "print(json.load(...)['claudeAiOauth']['accessToken'])"` and writing ONLY that
one field loses `refreshToken` and `expiresAt`. A partial reconstruction later can still
LOOK complete (has a `refreshToken` key) while `expiresAt` reads `0` -- and `claude` treats
`0` as "already expired," attempts a refresh, and on ANY refresh failure just prints
`Failed to authenticate: OAuth session expired and could not be refreshed` and exits 1. No
retry, no fallback account tried (because `account_pool_run`'s own subshell already
absorbed the exit code before classification), and worse: `worktree_builder.sh`'s
`log "item #$ID: build session failed rc=$RC"` line never even fired in one observed run --
the failure happened fast enough, and early enough in the pipeline, that the log write and
the `claude` process both raced the parent script's exit. Net effect: the builder silently
did nothing, twice, with zero trace beyond "no PR found."

**Fix: copy the WHOLE credentials file, never reconstruct a subset of fields.**
`security find-generic-password -s "Claude Code-credentials" -w` on macOS (or the plain
`~/.claude/.credentials.json` file on Linux) dumped verbatim to
`$HOME/.claude-<account>/.credentials.json` on the target box. Verify with:
```
python3 -c "import json; c=json.load(open(PATH)); o=c['claudeAiOauth']; print(o.get('expiresAt'), 'refreshToken' in o)"
```
A real `expiresAt` (a large future epoch-ms integer, not `0`) is the tell that the copy is
whole. Sanity-check with a live `claude -p "say OK" --model haiku` call before trusting the
credential for anything unattended -- this is the same check `entrypoint.sh`'s per-account
warning does at container boot, but it only checks the file EXISTS, not that it's a complete,
working credential. Worth hardening `entrypoint.sh` to make that same live call, not just a
file-existence check, next time this surface gets touched.

## 11. A target repo's own CLAUDE.md/persona convention silently hijacks an unattended
   `claude -p` call — the REAL cause of three "did nothing" build attempts

Three straight attempts against a real backlog item (nonprofit-atlas #3050) all "did
nothing": no file changes, no PR, no error, log frozen right after worktree creation.
Chased it through a permission-flag bug (real, fixed, learning #6) and a stale-credential
bug (real, fixed, learning #9) before finding the actual cause on attempt 4: the TARGET
repo's own `CLAUDE.md` instructs any Claude Code session with no persona explicitly
assigned to become "M," its own orchestrator persona — and that repo's own PreToolUse hook
then correctly BLOCKS "M" from editing code (by that repo's own design, M is meant to
delegate, never write). So every build session correctly, silently became a persona that
was correctly, silently forbidden from doing the job -- with no error at any layer, because
every layer was working exactly as its own repo intended.

Proven live:
```
$ claude -p "who are you, one line" --dangerously-skip-permissions          # project settings loaded
I am **M** — the orchestrator for 990 Scout. ...

$ claude -p "who are you, one line" --dangerously-skip-permissions --setting-sources user
I'm Claude, an AI agent built by Anthropic to help you with software engineering tasks...
```
`--settings '{"hooks":{}}'` was tried first and did NOT fix it — the identity comes from
CLAUDE.md's own project instructions, not only the SessionStart hook that injects it, so
suppressing hooks alone is insufficient.

**Fix: `--setting-sources user` on every `claude -p` invocation in this kit.** Keeps
user-level config (auth, model preference) while dropping project/local settings (the
target repo's CLAUDE.md, hooks, `.claude/settings.json`) — so this kit's own injected
charter (`builder.md` / the CEO/architect charter) is what actually governs the session,
regardless of what identity convention the target repo happens to run. Applied to both
`worktree_builder.sh` and `run_agent_pass.sh`.

**This is a real trap for ANY multi-agent-fleet repo used as a build target, not specific
to one project** — any repo whose CLAUDE.md says "you are persona X by default" will hit
this exact failure mode against an external automation tool that doesn't know to disclaim
that identity. Worth the flag on every invocation by default, not just as a fix-when-hit.

## 12. `worktree_builder.sh`'s log goes silent on a hung subprocess — no heartbeat, no timeout
   surfaced to the log

When the builder's `claude -p` call hung on the stray-SSH issue above, `/tmp/builder_run.log`
simply stopped growing — no "still running", no partial-timeout warning, nothing
distinguishing "working on something slow" from "wedged". Worth hardening in a future pass:
periodic heartbeat lines while the build subprocess runs, and an explicit log line the moment
`$TIMEOUT_S` is hit rather than only the process disappearing.
