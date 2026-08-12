# Schedulers

Templates for both macOS (launchd) and Linux (systemd timer). Fill the `{{...}}` placeholders
— `{{REPO_PATH}}` (your `FLEET_REPO`), `{{KIT_PATH}}` (where this kit lives, e.g. `{{REPO_PATH}}/.fleet`),
`{{USER}}` (the account running the fleet) — then install per your platform's normal mechanism.

| Job | Cadence | Script | Required? |
|---|---|---|---|
| gitpull | 5–15 min | `git -C $FLEET_REPO pull --ff-only` — one line, no dedicated script needed | if the fleet runs on a persistent box rather than cloning fresh each tick |
| rank | hourly | your own `gh issue list` + RICE pass (not shipped — see README's "Rank" note) | optional; without it the backlog stays FIFO |
| build | hourly | `scripts/worktree_builder.sh` | **required** |
| review | 15 min | `scripts/code_review_local.sh` | **required** if you don't have another review gate |
| ceo | hourly | your `agents/ceo.md`-driven pass | optional; the fleet still runs without one, just with no self-healing |
| architect | daily | your `agents/architect.md`-driven pass | optional; without it the fleet only ever ships PR-sized increments, never features |

launchd: `cp <file> ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/<file>`
systemd: `cp <file>.service <file>.timer /etc/systemd/system/ && systemctl enable --now <file>.timer`

Both template families set `PATH`/environment explicitly — neither launchd nor a systemd
timer gives a job a login shell, so `gh`/`git`/`claude` are not guaranteed to be found
otherwise. Adjust the PATH entries to wherever those binaries actually live on your box.
