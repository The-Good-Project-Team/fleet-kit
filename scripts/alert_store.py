#!/usr/bin/env python3
"""alert_store.py -- the one place a fleet alarm's STATE lives, so that (a) a human is paged
once per real problem instead of once per cron tick, and (b) the fleet itself can read what is
currently wrong and pace against it.

WHY THIS EXISTS (2026-09-05, 27 alarms in 48h, 3 of them real):

Every check on this box decided ON ITS OWN to send mail. That single design choice caused
both of the failures this module fixes:

  1. NO DEDUPE ACROSS CHECKS. anchor_staleness_check latches per-handle, so one maxx outage
     paged 11 times -- twice per 5-minute tick (once for reif, once for reif_tgp) plus a
     "recovered" note. Each latch was individually correct and collectively spam.

  2. TRANSIENT != FAULT. budget_read_check pages when it cannot read the meter. But it reads
     the meter THROUGH `podman exec`, so a container that is merely restarting is
     indistinguishable from a corrupt meter. On 2026-09-05 02:45 UTC the philanthropy
     container was down for ~6 minutes during a clean restart; the check paged "meter
     UNREADABLE (label=parse_fail)" naming the WRONG account, and the next tick logged
     RECOVERED. A human was woken for a condition that healed itself in 15 minutes.

The deeper cost of (2) is not the lost sleep, it is that noise is how a real alarm gets
ignored. The founding incident (60h of a stale anchor, one whole account unused) was found by
a human squinting at a usage screenshot. An inbox with 24 false pages in it is a worse
detector than no pages at all, because it trains the reader to archive on sight.

SEVERITY IS THE WHOLE DESIGN. Checks no longer decide whether to page; they report a condition
with a severity, and this module decides:

  transient -- the check could not observe the thing (container down, HTTP 5xx, DNS blip,
               timeout). This is NOT evidence the watched thing is broken; it is evidence we
               are blind. NEVER pages on its own. Escalates to degraded only if we stay blind
               past TRANSIENT_ESCALATE_SEC (default 30m) -- a container down for half an hour
               is no longer a restart, it is an outage.

  degraded   -- really wrong, but self-healing or bounded (a 5h block wall, an anchor drifting
               past its limit but still fresh enough to use). Pages once, only after it has
               been continuously true for DEGRADED_MIN_SEC (default 2 consecutive runs), so a
               condition that clears on the next tick never reaches a human.

  critical   -- broken and staying broken without a person: revoked/rejected auth, every
               account failing, an anchor so old the numbers are fiction. Pages IMMEDIATELY,
               first observation, no debounce. Cutting page latency for these is the entire
               point of accepting silence on the other two.

DEDUPE IS ON (check, handle, problem), GLOBALLY. One JSON file, one lock, every check writing
into it. Two scripts observing the same maxx outage for two handles collapse to one open
alarm, because the (check, problem) key matches even though the handle differs -- the handle
is recorded in `handles` rather than being part of the identity. That is the specific bug
that turned one outage into 11 emails.

FAIL-OPEN, ALWAYS. Every public function swallows its own exceptions and degrades to "page
the human the old way" rather than raising. A bug in the alarm de-duplicator must never be
able to take down the check that called it -- that would reintroduce, at a higher level, the
exact silent-failure class the checks exist to catch. There is no failure of this module that
should ever result in FEWER pages than the dumb version.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1

# A condition we could not observe has to persist this long before we admit it is an outage
# rather than a restart. The philanthropy container's clean restart took ~6 minutes end to
# end, so anything under ~10m is normal operations; 30m is comfortably past that and still
# well inside the 60h that the founding incident went unnoticed.
TRANSIENT_ESCALATE_SEC = int(os.environ.get("FLEET_ALERT_TRANSIENT_ESCALATE_SEC", "1800"))

# A degraded condition must be seen continuously for this long before paging. Checks run every
# 5-15 minutes, so 10m means "seen on at least two consecutive runs" -- enough to drop
# anything that clears itself on the next tick.
DEGRADED_MIN_SEC = int(os.environ.get("FLEET_ALERT_DEGRADED_MIN_SEC", "600"))

# How long a resolved alarm stays visible in the feed. The fleet polls this to self-heal, and
# a consumer that polls every few minutes still needs to see "this just recovered" so it can
# release a conservative pace it adopted while the alarm was open.
RESOLVED_RETAIN_SEC = int(os.environ.get("FLEET_ALERT_RESOLVED_RETAIN_SEC", "3600"))

SEVERITIES = ("transient", "degraded", "critical")


def _state_path() -> Path:
    log_dir = os.environ.get("FLEET_LOG_DIR", "/home/ubuntu/fleet-kit-logs")
    return Path(os.environ.get("FLEET_ALERT_STATE_FILE", str(Path(log_dir) / "alerts.json")))


def _now() -> float:
    return time.time()


def _load(path: Path) -> dict:
    try:
        with path.open() as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"version": SCHEMA_VERSION, "alerts": {}}
        data.setdefault("alerts", {})
        return data
    except FileNotFoundError:
        return {"version": SCHEMA_VERSION, "alerts": {}}
    except Exception:
        # Corrupt state must not wedge the alarm path. Start clean; the worst case is one
        # duplicate page, which is strictly better than an exception that kills the check.
        return {"version": SCHEMA_VERSION, "alerts": {}}


def _save(path: Path, data: dict) -> None:
    """Atomic write. A half-written state file read by the next tick would look like a fresh
    alarm and re-page, so the rename has to be the only thing a reader can observe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    tmp.replace(path)


class _Lock:
    """Coarse file lock. Six checks on 2/5/10/15-minute crons land on the same minute
    regularly; without this, two of them read-modify-write the same file and one alarm's
    latch silently vanishes -- which would re-page. Never blocks forever: a stale lock is
    stolen after `timeout` so a crashed check cannot mute the pager permanently."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path.with_suffix(".lock")
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        deadline = _now() + self.timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if _now() > deadline:
                    # Steal it. A lock older than the timeout means the holder died mid-write.
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                time.sleep(0.1)
            except Exception:
                return self  # never let locking itself break the caller

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.path.unlink()
        except Exception:
            pass
        return False


def record(check: str, problem: str, severity: str = "degraded", handle: str = "",
           detail: str = "", state_file: str | None = None) -> dict:
    """Record that `check` currently observes `problem`. Returns a verdict dict whose
    "page" key is the ONLY thing the caller should use to decide whether to send mail.

    The caller passes severity; this decides timing. Returned keys:
      page        -- bool, send the alert now
      reason      -- why we did or did not page (goes in the check's log)
      first_seen  -- when this condition was first observed
      severity    -- effective severity, which may be escalated above what was passed
    """
    try:
        if severity not in SEVERITIES:
            severity = "degraded"
        path = Path(state_file) if state_file else _state_path()
        key = f"{check}::{problem}"
        now = _now()

        with _Lock(path):
            data = _load(path)
            alerts = data["alerts"]
            rec = alerts.get(key)

            if rec is None or rec.get("resolved_at"):
                # New condition, or a previously-resolved one recurring. Either way this is a
                # fresh occurrence and the debounce clock restarts.
                rec = {
                    "check": check,
                    "problem": problem,
                    "severity": severity,
                    "handles": [handle] if handle else [],
                    "detail": detail,
                    "first_seen": now,
                    "last_seen": now,
                    "count": 1,
                    "paged_at": None,
                    "resolved_at": None,
                }
            else:
                rec["last_seen"] = now
                rec["count"] = int(rec.get("count", 0)) + 1
                rec["detail"] = detail or rec.get("detail", "")
                # Severity can rise but never fall while an alarm is open: a condition that
                # reports critical once is critical until it resolves, even if a later tick
                # observes it through a blinder that only proves transient.
                if SEVERITIES.index(severity) > SEVERITIES.index(rec.get("severity", "degraded")):
                    rec["severity"] = severity
                if handle and handle not in rec.get("handles", []):
                    rec.setdefault("handles", []).append(handle)

            effective = rec["severity"]
            age = now - rec["first_seen"]
            already_paged = rec.get("paged_at") is not None

            # Being blind for long enough IS an outage. Escalate so it can page.
            #
            # Escalation must page NOW, not enter the degraded debounce: this condition has
            # already been continuously true for TRANSIENT_ESCALATE_SEC (30m), which is far
            # past what the debounce is asking for. Making it wait another DEGRADED_MIN_SEC
            # would add 10 minutes of silence to an outage that has already earned a page --
            # the debounce exists to filter conditions we have only just seen, and this one
            # we have been staring at for half an hour.
            escalated = False
            if effective == "transient" and age >= TRANSIENT_ESCALATE_SEC:
                effective = "degraded"
                rec["severity"] = "degraded"
                rec["escalated_from_transient"] = True
                escalated = True

            if already_paged:
                page, reason = False, f"already paged for this condition ({int(age)}s open)"
            elif effective == "critical":
                page, reason = True, "critical -- paging on first observation"
            elif effective == "transient":
                page, reason = False, (
                    f"transient ({int(age)}s) -- cannot observe, not paging until "
                    f"{TRANSIENT_ESCALATE_SEC}s"
                )
            elif escalated:
                page, reason = True, (
                    f"blind for {int(age)}s (>{TRANSIENT_ESCALATE_SEC}s) -- "
                    "no longer a restart, paging"
                )
            elif age >= DEGRADED_MIN_SEC:
                page, reason = True, f"degraded and persistent ({int(age)}s) -- paging"
            else:
                page, reason = False, (
                    f"degraded but young ({int(age)}s < {DEGRADED_MIN_SEC}s) -- "
                    "waiting for the next run to confirm"
                )

            if page:
                rec["paged_at"] = now
            alerts[key] = rec
            _save(path, data)

        return {"page": page, "reason": reason, "first_seen": rec["first_seen"],
                "severity": effective, "count": rec["count"]}
    except Exception as exc:  # noqa: BLE001
        # Fail OPEN: if the store is broken we page, because the alternative is a muted pager.
        return {"page": True, "reason": f"alert_store failed ({type(exc).__name__}) -- paging",
                "first_seen": _now(), "severity": severity, "count": 1}


def resolve(check: str, problem: str, state_file: str | None = None) -> dict:
    """Mark a condition healed. Returns {"was_open": bool, "was_paged": bool} so the caller
    can send a recovery note ONLY if a human was actually told about the problem -- a
    "recovered" mail for an alarm that never paged is pure noise."""
    try:
        path = Path(state_file) if state_file else _state_path()
        key = f"{check}::{problem}"
        now = _now()
        with _Lock(path):
            data = _load(path)
            rec = data["alerts"].get(key)
            if rec is None or rec.get("resolved_at"):
                return {"was_open": False, "was_paged": False}
            rec["resolved_at"] = now
            was_paged = rec.get("paged_at") is not None
            data["alerts"][key] = rec
            _save(path, data)
        return {"was_open": True, "was_paged": was_paged}
    except Exception:
        return {"was_open": False, "was_paged": False}


def resolve_check(check: str, keep: set[str] | None = None,
                  state_file: str | None = None) -> list[dict]:
    """Resolve every open alarm for `check` except those whose problem is in `keep`.

    This is what a check calls when it completes a healthy run: it does not have to remember
    what it complained about last time, it just declares its current truth and everything else
    for that check closes. Without this, a check whose failure message changes wording (say,
    an anchor age in the text) would leak one permanently-open alarm per distinct wording."""
    keep = keep or set()
    out = []
    try:
        path = Path(state_file) if state_file else _state_path()
        now = _now()
        with _Lock(path):
            data = _load(path)
            for key, rec in data["alerts"].items():
                if rec.get("check") != check or rec.get("resolved_at"):
                    continue
                if rec.get("problem") in keep:
                    continue
                rec["resolved_at"] = now
                out.append({"problem": rec.get("problem", ""),
                            "was_paged": rec.get("paged_at") is not None})
            if out:
                _save(path, data)
    except Exception:
        return []
    return out


def snapshot(state_file: str | None = None) -> dict:
    """Machine-readable current state -- what the fleet polls to self-heal.

    `open` is what is wrong RIGHT NOW. `budget_safe` is the single boolean a member should
    branch on: false means some alarm says the budget signal cannot be trusted, so pace
    conservatively rather than believing a headroom number computed from it. That is the
    direct fix for the founding incident, where gru fell back to a hardcoded constant and
    nothing anywhere told it the constant was wrong."""
    try:
        path = Path(state_file) if state_file else _state_path()
        data = _load(path)
        now = _now()
        open_alerts, recent = [], []
        for rec in data.get("alerts", {}).values():
            item = {
                "check": rec.get("check", ""),
                "problem": rec.get("problem", ""),
                "severity": rec.get("severity", "degraded"),
                "handles": rec.get("handles", []),
                "detail": rec.get("detail", ""),
                "first_seen": rec.get("first_seen"),
                "last_seen": rec.get("last_seen"),
                "count": rec.get("count", 0),
                "paged": rec.get("paged_at") is not None,
                "age_sec": int(now - (rec.get("first_seen") or now)),
            }
            if rec.get("resolved_at"):
                if now - rec["resolved_at"] <= RESOLVED_RETAIN_SEC:
                    item["resolved_at"] = rec["resolved_at"]
                    recent.append(item)
            else:
                open_alerts.append(item)

        open_alerts.sort(key=lambda a: SEVERITIES.index(a["severity"]), reverse=True)
        # Anything open that is not merely "we are blind" means the budget numbers may be
        # fiction. Transient stays out of this on purpose: being unable to reach the meter for
        # one tick is not a reason for the whole fleet to throttle.
        budget_unsafe = [a for a in open_alerts if a["severity"] in ("degraded", "critical")]
        return {
            "version": SCHEMA_VERSION,
            "generated_at": now,
            "open": open_alerts,
            "recently_resolved": recent,
            "counts": {s: sum(1 for a in open_alerts if a["severity"] == s) for s in SEVERITIES},
            "budget_safe": not budget_unsafe,
            "worst": open_alerts[0]["severity"] if open_alerts else "ok",
        }
    except Exception as exc:  # noqa: BLE001
        # A snapshot that cannot be built must say so rather than returning a healthy-looking
        # empty object -- a consumer reading {"budget_safe": true} from a broken store would
        # be exactly the silent fail-open this whole system exists to prevent.
        return {"version": SCHEMA_VERSION, "generated_at": _now(), "open": [],
                "recently_resolved": [], "counts": {}, "budget_safe": False,
                "worst": "unknown", "error": f"{type(exc).__name__}: {exc}"}


def prune(state_file: str | None = None) -> int:
    """Drop resolved alarms past the retention window so the file cannot grow forever."""
    try:
        path = Path(state_file) if state_file else _state_path()
        now = _now()
        removed = 0
        with _Lock(path):
            data = _load(path)
            alerts = data.get("alerts", {})
            for key in [k for k, r in alerts.items()
                        if r.get("resolved_at") and now - r["resolved_at"] > RESOLVED_RETAIN_SEC]:
                del alerts[key]
                removed += 1
            if removed:
                _save(path, data)
        return removed
    except Exception:
        return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="fleet alert state store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record", help="record a condition; prints PAGE or SKIP")
    p_rec.add_argument("--check", required=True)
    p_rec.add_argument("--problem", required=True)
    p_rec.add_argument("--severity", default="degraded", choices=SEVERITIES)
    p_rec.add_argument("--handle", default="")
    p_rec.add_argument("--detail", default="")

    p_res = sub.add_parser("resolve", help="mark one condition healed")
    p_res.add_argument("--check", required=True)
    p_res.add_argument("--problem", required=True)

    p_rc = sub.add_parser("resolve-check", help="resolve all open alarms for a check")
    p_rc.add_argument("--check", required=True)
    p_rc.add_argument("--keep", default="", help="problem string to keep open")

    sub.add_parser("snapshot", help="print current state as JSON")
    sub.add_parser("prune", help="drop old resolved alarms")

    args = ap.parse_args()
    if args.cmd == "record":
        v = record(args.check, args.problem, args.severity, args.handle, args.detail)
        print(f"{'PAGE' if v['page'] else 'SKIP'} {v['reason']}")
        raise SystemExit(0 if v["page"] else 10)
    if args.cmd == "resolve":
        v = resolve(args.check, args.problem)
        print(json.dumps(v))
    elif args.cmd == "resolve-check":
        keep = {args.keep} if args.keep else set()
        print(json.dumps(resolve_check(args.check, keep)))
    elif args.cmd == "snapshot":
        print(json.dumps(snapshot(), indent=2))
    elif args.cmd == "prune":
        print(json.dumps({"pruned": prune()}))
