#!/usr/bin/env python3
"""Validate a /etc/cron.d-style file BEFORE cron sees it.

Vixie cron rejects the ENTIRE file when any single time field is out of range --
it does not skip the offending line. On 2026-09-03 `FLEET_GRU_CADENCE=0,30` (meant
as "every 30 minutes", but this dial feeds the HOUR field) produced `3 0,30 * * *`.
Hour 30 is out of range, so all 18 fleet jobs went silent at once for ~40h while
`cron -f` sat there looking healthy.

Usage:
  validate_crontab.py FILE            -> exit 1 and list bad lines
  validate_crontab.py FILE --fix      -> comment out bad lines in place, exit 0
"""
import re
import sys

RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DAYS = "sun mon tue wed thu fri sat".split()


def _num(tok, idx):
    t = tok.strip().lower()
    if idx == 3 and t in MONTHS:
        return MONTHS.index(t) + 1
    if idx == 4 and t in DAYS:
        return DAYS.index(t)
    if not re.fullmatch(r"\d+", t):
        return None
    return int(t)


def field_ok(field, idx):
    lo, hi = RANGES[idx]
    for part in field.split(","):
        part = part.strip()
        if not part:
            return False
        if "/" in part:
            part, _, step = part.partition("/")
            if not re.fullmatch(r"\d+", step) or int(step) == 0:
                return False
            if part == "*":
                continue
        if part == "*":
            continue
        if "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            na, nb = _num(a, idx), _num(b, idx)
            if na is None or nb is None:
                return False
            if not (lo <= na <= hi and lo <= nb <= hi):
                return False
            continue
        n = _num(part, idx)
        if n is None or not (lo <= n <= hi):
            return False
    return True


def check(path):
    bad = []
    lines = open(path).read().split("\n")
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        # env assignment (NAME=value) before any whitespace
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", s):
            continue
        parts = s.split()
        if s.startswith("@"):
            continue
        if len(parts) < 6:
            bad.append((i, raw, "fewer than 6 fields (5 time fields + user)"))
            continue
        for idx in range(5):
            if not field_ok(parts[idx], idx):
                bad.append((i, raw, "invalid %s field %r" % (NAMES[idx], parts[idx])))
                break
    return lines, bad


def main():
    if len(sys.argv) < 2:
        print("usage: validate_crontab.py FILE [--fix]", file=sys.stderr)
        return 2
    path = sys.argv[1]
    fix = "--fix" in sys.argv[2:]
    lines, bad = check(path)
    if not bad:
        print("crontab OK: %s" % path)
        return 0
    for ln, raw, why in bad:
        print("INVALID line %d: %s" % (ln, why), file=sys.stderr)
        print("    %s" % raw.strip()[:140], file=sys.stderr)
    if not fix:
        return 1
    for ln, _, why in bad:
        lines[ln - 1] = "# QUARANTINED by validate_crontab.py (%s): %s" % (why, lines[ln - 1])
    open(path, "w").write("\n".join(lines))
    print("quarantined %d bad line(s) -- the remaining jobs still load" % len(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
