#!/usr/bin/env python3
"""authority.py -- gh#771, the authority ladder's first buildable seam (#558 step 2).

`scripts/ask.py` (gh#568) files an ask and records its answer, but every ask is filed at the
same level forever: a member that asked "may I rotate this credential?" and got "yes" on
Monday files the identical ask on Tuesday, and again on Wednesday. Nothing reads past answers.
This is the file that lets a human say "always, for this kind of thing" once, so the same
class of question stops reaching them -- Reif, on why #558 exists at all: "it's learning from
me, and giving me less and less things -- because I will give it more and more authority."

Three levels, named in #558's own build list:
  ask           -- today's behavior. File and stop. The default for every class, always.
  act-and-tell  -- proceed, file a notice (not a question) for the record.
  act           -- proceed, fully authorized; a countable record is still filed.

WHERE THIS FILE LIVES, AND WHY (the PRD's own open question): the same place
`scripts/overrides.py` puts its live-tunable dials -- under `FLEET_LOG_DIR`, not under
Claude Code's own managed config directory. gh#51 found that second location gets swept by
something never identified, silently reverting state within hours; `FLEET_LOG_DIR` is the
fleet-owned, container-mounted, already-persistent directory `runs.jsonl` itself lives in (see
deploy.sh's logs bind-mount), so a grant survives a container rebuild the same way a run's
history does. `FLEET_AUTHORITY_PATH` still wins outright if set.

NON-GOALS (per marie's PRD on gh#771): no UI of any kind, no auto-promotion (detecting a
repeated identical answer and granting on its own is #558 step 6, datta's memo), no migration
of `fleet:needs-human-op` issues, no new ask classes -- `ask.ASK_CLASSES` is untouched. And
per the PRD's own flagged-not-solved point: nothing here authenticates who is calling `grant()`
-- the file is operator-controlled and the fleet runs on one token today. That stops being
safe once a promotion button is exposed in superadmin; this issue does not build that button.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LEVELS = ("ask", "act-and-tell", "act")

_LOG_DIR = Path(os.environ.get("FLEET_LOG_DIR", Path.home() / "Library" / "Logs" / "fleet-kit")).expanduser()
STORE = Path(os.environ.get("FLEET_AUTHORITY_PATH", _LOG_DIR / "authority.json")).expanduser()


class AuthorityError(ValueError):
    """A grant, or the file itself, names a class or level this repo does not define.

    Raised, never swallowed into "ask" -- AC6: a malformed authority file must fail loudly,
    the same way a malformed override row is refused at write time by overrides.py, not
    silently downgraded to the safe default. Downgrading here would hide the exact class of
    bug this file exists to prevent: a typo'd class silently never granting real authority,
    discovered only when someone wonders why the ladder "isn't working."
    """


def _ask_classes():
    # Deferred import: ask.py imports this module at top level (to decide file_ask's status),
    # so importing ask.py back at authority.py's own top level would be circular. By the time
    # any function here actually runs, ask.py (if it's the caller) is already in sys.modules,
    # and a standalone caller (tests, the CLI below) pays one extra import, not a cycle.
    import ask
    return ask.ASK_CLASSES


@contextlib.contextmanager
def _locked(store: Path):
    """Exclusive, blocking lock around a read-modify-write cycle -- same pattern
    maxx_lease.py's `_locked` uses for its own flat-file ledger: two concurrent `grant()` calls
    (a member and an operator, or two operators) must not both read the pre-grant file and
    silently clobber each other's write."""
    lock_path = store.with_suffix(store.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _validate(data: dict) -> None:
    valid_classes = _ask_classes()
    for ask_class, row in data.items():
        if ask_class not in valid_classes:
            raise AuthorityError(
                f"authority.json: unknown ask class {ask_class!r} "
                f"(known: {', '.join(valid_classes)})")
        level = row.get("level") if isinstance(row, dict) else None
        if level not in LEVELS:
            raise AuthorityError(
                f"authority.json: {ask_class!r} has invalid level {level!r} "
                f"(known: {', '.join(LEVELS)})")


def _read(store: Path) -> dict:
    """AC1: a missing or unparseable file always reads as {} -- no grant for anyone, so every
    class stays at 'ask'. AC6: a file that PARSES but names an unknown class or level is a
    different failure -- someone wrote a real grant wrong -- and that one is never swallowed.

    gh#888: a file that exists but can't be READ (permissions, a path pointing at a directory,
    an NFS hiccup) is the same "no grant" case as missing, per the class's own docstring above
    -- it must degrade to {} too, not propagate as a traceback that stops the ask from ever
    being filed. Unlike missing (silently normal), an unreadable file is an operator problem,
    so it gets one stderr line -- everything in OSError except FileNotFoundError itself.
    """
    try:
        raw = store.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        print(f"authority.py: {store}: {type(exc).__name__}: {exc} -- treating as no grants",
              file=sys.stderr)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    _validate(data)
    return data


def _write(store: Path, data: dict) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(store.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(store)


def level_for(ask_class: str | None, *, store: Path | None = None) -> str:
    """The level in force for one class. AC1: no class, no file, or no grant for this class
    all mean 'ask' -- the only default that can never accidentally authorize an action."""
    if not ask_class:
        return "ask"
    row = _read(store or STORE).get(ask_class)
    return row["level"] if row else "ask"


def grant(ask_class: str, level: str, *, by: str, ask_ids: list[int] | None = None,
         granted_at: float | None = None, store: Path | None = None) -> dict:
    """Record a standing grant for one class. AC7: overwrites any prior grant for that class;
    re-reading the file returns exactly this row."""
    valid_classes = _ask_classes()
    if ask_class not in valid_classes:
        raise AuthorityError(
            f"grant: unknown ask class {ask_class!r} (known: {', '.join(valid_classes)})")
    if level not in LEVELS:
        raise AuthorityError(f"grant: unknown level {level!r} (known: {', '.join(LEVELS)})")
    if not by or not by.strip():
        raise AuthorityError("grant: needs who granted it -- that is what makes it auditable")
    store = store or STORE
    row = {
        "level": level,
        "granted_by": by.strip(),
        "granted_at": granted_at if granted_at is not None else time.time(),
        "ask_ids": sorted(set(ask_ids or [])),
    }
    with _locked(store):
        data = _read(store)
        data[ask_class] = row
        _write(store, data)
    return row


def show(*, store: Path | None = None) -> dict:
    """AC5: every current grant, ask ids included, so a human can audit what a standing
    permission was granted on the basis of."""
    return _read(store or STORE)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Read or write the authority ladder's per-class grants (gh#771).")
    ap.add_argument("--path", help="override the authority.json path (default: $FLEET_AUTHORITY_PATH or FLEET_LOG_DIR/authority.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_grant = sub.add_parser("grant", help="record a standing grant for one ask class")
    p_grant.add_argument("--class", dest="ask_class", required=True)
    p_grant.add_argument("--level", required=True, choices=LEVELS)
    p_grant.add_argument("--by", required=True, help="who is granting this")
    p_grant.add_argument("--ask-ids", default="", help="comma-separated ask ids that justified the grant")

    sub.add_parser("show", help="print every current grant as JSON")

    a = ap.parse_args(argv)
    store = Path(a.path) if a.path else None

    if a.cmd == "grant":
        ask_ids = [int(x) for x in a.ask_ids.split(",") if x.strip()]
        try:
            row = grant(a.ask_class, a.level, by=a.by, ask_ids=ask_ids, store=store)
        except AuthorityError as exc:
            print(f"authority.py: {exc}", file=sys.stderr)
            return 2
        print(f"{a.ask_class} -> {row['level']} (by {row['granted_by']}, "
              f"ask_ids={row['ask_ids']})")
        return 0

    if a.cmd == "show":
        try:
            print(json.dumps(show(store=store), indent=2, sort_keys=True))
        except AuthorityError as exc:
            print(f"authority.py: {exc}", file=sys.stderr)
            return 2
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
