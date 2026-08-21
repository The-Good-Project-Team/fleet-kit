# Messenger driver contract

Provenance: distilled from nonprofit-atlas's scripts/lucky2/logship.py + transcript_relay.py +
gitpull_stall_alert.py — three scripts, one shape (tail local state, POST it to a dashboard
via a bearer token). This kit ships no destination, same reasoning as deploy_driver.md: "where
your dashboard lives" is the single most product-specific thing in this whole persona.

## The three functions

A messenger driver is any script implementing these three operations. Wire the path into
`FLEET_MESSENGER_DRIVER` in `fleet.env`, or call it directly from `dont-shoot-the-messenger.sh`.

### `ship_logs <log_dir>`
Tails every log file under `log_dir` for NEW bytes since its own last run and delivers them
somewhere durable (a dashboard ingest endpoint, a log aggregator, even just a consolidated
file). Must be resumable: track a per-file offset so a restart never re-ships or drops bytes.

```
messenger_driver.sh ship_logs /var/log/fleet-kit
# -> exit 0 (shipped or nothing new), 1 (destination unreachable -- retry next tick), 2 (config error)
```

### `list_pending`
Prints every run_id currently awaiting a transcript delivery, one per line (empty output if
none). This is the driver's own state to enumerate — the caller never reaches into the
driver's storage directly, only ever asks "what's pending" then relays each one.

```
messenger_driver.sh list_pending
# -> zero or more run_ids, one per line, on stdout
```

### `relay_transcript <run_id>`
Delivers one run's full transcript on request (a human or another system asking "what did
gru actually do on run X"). The BLIND case matters: if the transcript genuinely can't be
found (never recorded, unreadable, network down), the driver must still report that
explicitly — never silence, and never a stale/wrong transcript standing in for "not found".

```
messenger_driver.sh relay_transcript <run_id>
# -> exit 0 (delivered, found-or-explicitly-not-found), nonzero (transient failure, retry next tick)
```

## Properties any real driver needs (both real source scripts learned these the hard way)

- **Bounded catch-up, not unbounded replay.** A backlog exceeding your per-file cap ships only
  the newest cap-sized tail and records how many bytes were skipped — silent unbounded replay
  can outrun your own ingest capacity on a big backlog.
- **Never take the auth token as an argv value.** `ps` shows argv to every user on the box —
  read it from an env var or a file, same rule as GH_TOKEN elsewhere in this kit.
- **Best-effort, never blocks the caller.** A messenger failure must never change the exit code
  of whatever it's reporting on behalf of — it's a side channel, not a gate.

## Wiring it into the loop

`dont-shoot-the-messenger` (mechanical, see its own run.sh) calls both functions on its own
cadence. No driver configured (`FLEET_MESSENGER_DRIVER` unset) means logs stay local-only and
transcript relay is unavailable — a perfectly valid mode for a fleet with no dashboard yet.
