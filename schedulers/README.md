# Schedulers

Templates for both macOS (launchd) and Linux (systemd timer). Fill the `{{...}}` placeholders
— `{{REPO_PATH}}` (your `FLEET_REPO`), `{{KIT_PATH}}` (where this kit lives, e.g. `{{REPO_PATH}}/.fleet`),
`{{USER}}` (the account running the fleet), `{{LOG_DIR}}` (your `FLEET_LOG_DIR`),
`{{WEBHOOK_PORT}}` (your `FLEET_WEBHOOK_PORT`, e.g. `8562`), `{{NTFY_TOPIC}}` (the ntfy.sh
topic the health pagers below page to), `{{PUBLIC_URL}}` (the public tunnel URL
tunnel-health checks, e.g. `https://your-fleet.example.com/`), `{{VIEW_PORT}}` (your
`FLEET_VIEW_PORT`, e.g. `8420`), `{{INSTANCE_DIR}}` (this instance's state dir, e.g.
`instances/<name>` under `up.sh`'s own layout — where its `fleet.env` lives), and
`{{CONTAINER_NAME}}` (the podman container this instance runs as, e.g. `fleet-kit-<name>`), and `{{INSTANCE_NAME}}` (the label the member-liveness pager names in its page), and
`{{PHILANTHROPY_CF_TEST_HEADER_VALUE}}` (prod-health's Cloudflare bypass header value — a
secret a human provisions per philanthropy repo's `docs/ops/monitoring.md`, leave unset and
prod-health simply skips the one probe that needs it rather than false-paging) —
then install per your platform's normal mechanism.

| Job | Cadence | Script | Required? |
|---|---|---|---|
| gitpull | 5–15 min | `git -C $FLEET_REPO pull --ff-only` — one line, no dedicated script needed | if the fleet runs on a persistent box rather than cloning fresh each tick |
| rank | hourly | your own `gh issue list` + RICE pass (not shipped — see README's "Rank" note) | optional; without it the backlog stays FIFO |
| build | hourly | `scripts/worktree_builder.sh` | **required** |
| review | 15 min | `members/judge-judy/judge-judy.sh` | **required** if you don't have another review gate |
| ceo | hourly | your `agents/ceo.md`-driven pass | optional; the fleet still runs without one, just with no self-healing |
| architect | daily | your `agents/architect.md`-driven pass | optional; without it the fleet only ever ships PR-sized increments, never features |
| account-health | 5 min | `scripts/account_health_check.sh` | **required** — pages when every fleet account has failed for a sustained stretch; the only thing watching for a fully-dead account pool |
| member-liveness | 5 min | `scripts/member_liveness_check.sh` | **required** — the dead man's switch: pages when no member has landed an `ok` run in `LIVENESS_MAX_AGE_S` (default 3h), names the pool's reset time when that silence is genuine exhaustion. Host-side, per instance (`FLEET_LOG_DIR=instances/<name>/logs`). The only check that asserts the fleet did WORK, not that a process is alive (fleet-kit#512) |
| tunnel-health | 5 min | `scripts/tunnel_health_check.sh` | **required** if you expose the fleet through a public tunnel — self-heals ingress drift, pages when the public URL is not serving |
| path-health | hourly | `scripts/path_health_check.sh` | **required** if you route instances by path behind a reverse proxy (e.g. Caddy `/fleet/<name>`) — pages when an instance's own dashboard path stops returning 200, the one thing tunnel-health's root-hostname check can't see |
| sync-health | 5 min | `scripts/sync_health_check.sh` | **required** — pages when `fleet_view_server.py`'s `tail_runs_forever` thread has fallen behind `runs.jsonl`, the only thing keeping `fleet.db` in sync for nerd/gru/dumbledore's direct reads |
| pacing-hold | hourly | `scripts/pacing_hold_check.py` | **required** — pages when a real zero pacing ceiling (`maxx_share_ceiling.py`'s `block_over_pace` branch, or maxx's own `verdict=over`) holds the whole fleet `status=paced` for 2+ consecutive hourly ticks, distinct from the unreadable-meter case budget-read (below) already covers (gh#812) |
| prod-health | 5 min | `scripts/prod_health_check.py` | optional, but the ONLY external vantage point on philanthropy.org today (gh#727/#4898) — probes prod's home/search/report pages (each against an 8s latency budget) plus its app-canary heartbeat from THIS host, not from the box being watched, so a dead product-side cron or box doesn't take its own pager down with it. Two consecutive failures page AND file/update one deduped incident issue on the product repo, and comment (never auto-close) on recovery. Needs `PHILANTHROPY_CF_TEST_HEADER_VALUE` (a human-provisioned secret, see philanthropy repo's `docs/ops/monitoring.md`) to probe the report page — unset simply skips that one probe rather than false-paging. `fleetkit-prod-health.service`/`.timer` (or `com.fleetkit.prod-health.plist`) |
| view | always-on (not interval-scheduled) | `scripts/fleet_view_server.py` | optional — a live window onto `runs.jsonl` + `gh` state; kill it and the loop above is untouched. See `fleetkit-view.service` / `com.fleetkit.view.plist` (a long-running service, not a timer/interval job like the rest of this table). |

launchd: `cp <file> ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/<file>`
systemd: `cp <file>.service <file>.timer /etc/systemd/system/ && systemctl enable --now <file>.timer`
(the `view` job has no `.timer` — it's `Type=simple` + `Restart=on-failure`, enabled directly:
`systemctl enable --now fleetkit-view.service`)

## Host-only jobs: scripts that `podman exec` into the fleet's own container

The table above covers two shapes: jobs that run **inside** the container (entrypoint.sh's own
crontab — see its heredoc) and pure host-read pagers (account/tunnel/sync-health) that need
nothing but log files and a `curl` to page. Neither shape works for a script that reaches
*into* the running fleet container from outside it — the container has no podman socket bind
mounted, so it cannot `podman exec` itself (gh#376). These two jobs are that third shape, and
must be scheduled host-side, never added to entrypoint.sh's crontab:

| Job | Cadence | Script | Required? |
|---|---|---|---|
| account-heartbeat | hourly | `scripts/account_heartbeat.sh` | recommended — catches a pool account going stale with nothing else running to reveal it (the 60.2h incident its own header documents); costs one real `claude -p` call per pool account per tick |
| budget-read | 15 min | `scripts/budget_read_check.sh` | recommended — pages when the account the fleet is actually spending from goes budget-blind instead of failing open silently (the incident PR#338 shipped both scripts for) |

Both need `FLEET_INSTANCE_DIR` and `FLEET_CONTAINER_NAME` (`{{INSTANCE_DIR}}`/
`{{CONTAINER_NAME}}` above) in addition to the usual placeholders — see
`fleetkit-account-heartbeat.service`/`.timer` and `fleetkit-budget-read.service`/`.timer` (or
their launchd equivalents) for the exact shape. Cadence is a starting default, not a measured
optimum — see each `.timer`/`.plist`'s own comment for the open question a human should
confirm against real per-tick cost.

Both template families set `PATH`/environment explicitly — neither launchd nor a systemd
timer gives a job a login shell, so `gh`/`git`/`claude` are not guaranteed to be found
otherwise. Adjust the PATH entries to wherever those binaries actually live on your box.
