# Deploy driver contract

Provenance: distilled from nonprofit-atlas's blue-green deploy scripts (`docs/ops/deploy.md`
and the `scripts/box/deploy-*.sh` family) — the source fleet's driver was a two-instance
blue-green setup with a promote gate. This kit ships no deployer, on purpose: "how you
deploy" is the single most product-specific thing in the whole loop. What's generic is the
CONTRACT a fleet's CEO/CI pass calls against.

## The three functions

A deploy driver is any script (or set of scripts) implementing these three operations. Name
them however you like; wire the paths into `FLEET_DEPLOY_DRIVER` in `fleet.env` or call them
directly from your CI workflow.

### `current_sha`
Reports what SHA is actually serving right now. Must be a REAL read of the running system
(a `/health` endpoint, a file the deploy process writes, a running process's compiled-in
version) — never a guess from "we deployed X, so it must be running X." A deploy that
silently failed partway through is exactly the case this function exists to catch.

```
deploy_driver.sh current_sha
# -> prints a 40-char git SHA, or empty + nonzero exit if unreadable
```

### `deploy`
Takes the system from its current SHA to a target SHA. Two properties any real driver needs:
- **Idempotent**: calling it again with the same target when already there is a safe no-op,
  not a second deploy.
- **Fails loud**: a partial or failed deploy must leave `current_sha` still reporting the
  OLD (working) SHA, never a half-applied state that reads as success.

```
deploy_driver.sh deploy <target_sha>
# -> exit 0 on success (current_sha now == target_sha); nonzero + current_sha unchanged on failure
```

### `health`
An independent check that the deployed system is actually serving traffic correctly — not
just "the process is running," but "a real request gets a real response." This is what a
zero-downtime driver (blue-green, canary) gates promotion on before flipping live traffic.

```
deploy_driver.sh health
# -> exit 0 if healthy, nonzero + a one-line reason otherwise
```

## Example driver shapes (not shipped, for reference)

- **Single-server, systemd**: `deploy` = `git pull && systemctl restart app`; `health` = curl
  a `/health` route; `current_sha` = read a `RELEASE` file the app writes on boot.
- **Blue-green** (the source fleet's shape): `deploy` starts a NEW instance on a standby
  port with the target code, `health` checks the standby before anything flips, promotion
  (a separate step, not `deploy` itself) flips a reverse-proxy upstream and only then is the
  new SHA "current". Zero-downtime because the old instance keeps serving until the flip.
- **Managed platform** (Fly/Render/Vercel/a container registry): `deploy` triggers the
  platform's own deploy API; `health` polls the platform's own health-check status;
  `current_sha` reads back whatever tag/label the platform reports as live.

## Wiring it into the loop

The CEO pass (or your CI workflow directly) is the natural caller: after a merge, if
`current_sha` != the default branch's HEAD, call `deploy` with the new SHA, then `health`. No
driver configured (`FLEET_DEPLOY_DRIVER` unset) means the fleet builds/reviews/merges but
never deploys — a perfectly valid mode for a library or a repo with its own separate release
process.
