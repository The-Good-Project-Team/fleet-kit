# Prod diagnostic driver contract

Provenance: distilled from nonprofit-atlas's `scripts/lucky2/prod_diag.sh` (incident #2323,
2026-08-14: an 8-hour outage where CI and deploy stayed green while the site was dark, and the
on-call pass had a probe that could SEE the outage but nothing that could look at the box).
Same reasoning as `deploy_driver.md`: this kit ships no driver, because "how you read your own
production box" is exactly as product-specific as "how you deploy to it."

## The one function

A prod diagnostic driver is any script implementing this single operation. Wire its path into
`FIXER_PROD_DIAG_DRIVER` in `fleet.env`; `the-fixer` and `dumbledore` are the two members that
read it, both only on a confirmed PROD DOWN fire, never as a default grant.

### `<driver> <section>`
Runs a fixed, reviewable set of read-only probes against production and prints their raw
output to stdout. `section` is a name your driver defines (nonprofit-atlas's reference
implementation ships `app`, `pg`, `svc`, `log`, and `all`) — callers default to `pg` first,
since a wedged connection pool or a lock convoy is the most common cause a diagnosis is reached
for, and fall back to `all` when a single section doesn't explain the symptom.

```
prod_diag_driver.sh pg
# -> raw diagnostic text on stdout (connection/wait-state counts, whatever your probes cover);
#    exit 0 on a successful read even if the READING itself shows a problem -- nonzero exit
#    means the driver itself failed to run, not that production looks unhealthy.
```

**Read-only, by contract, not by convention.** The whole reason this is a pluggable driver
instead of a raw shell/SSH grant on every incident-response member is that a fixed, reviewed
script is safe to hand an autonomous pass; an open shell is not. A driver that can write to
production (a mutating SQL statement, a `systemctl restart`, anything beyond read-and-report)
defeats the one property this contract exists to guarantee — enforce read-only inside your own
driver (nonprofit-atlas's does this with `SET default_transaction_read_only = on` before every
query), not by trusting the caller not to ask for a mutation.

## Wiring it into the loop

`the-fixer`'s and `dumbledore`'s charters both check `FIXER_PROD_DIAG_DRIVER` on a PROD DOWN
fire before reasoning about a restore: configured means read first (paste the driver's raw
output into the fire record verbatim — a summary loses exactly the detail a human re-reads
this for later), then act; unconfigured means log the gap loudly and stop, never invent ad hoc
prod access. `the-fixer`'s own `check.sh` calls the driver itself the moment it detects
PROD DOWN (not the LLM pass) so the raw output already exists on disk by the time the charter
starts reasoning — see `check.sh`'s `FIXER_PROD_DIAG_DRIVER` block. No driver configured is a
perfectly valid mode; it just means an incident-response pass can name that a fire happened
but not what production looked like inside it.
