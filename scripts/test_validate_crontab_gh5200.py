"""Regression test for validate_crontab.py's empty-env-assignment gap (gh#5200): Vixie cron
discards the ENTIRE crontab file -- not just the offending line -- the moment it sees one env
assignment with an empty right-hand side (`FLEET_SHARE_DIR=` with nothing after the `=`). The
env-assignment branch used to skip every `NAME=...` line unconditionally, so a render that left
one variable empty passed validation clean and printed "crontab OK" against a file cron was
about to reject wholesale (the 2026-09-10 ~100-minute fleet-wide outage). Mutation-gate: run
this against the pre-fix `check()` (skip the `EMPTY_ENV_ASSIGNMENT_RE` branch) and
`test_empty_env_assignment_is_flagged` fails; against the fixed version it passes.

Run: python3 scripts/test_validate_crontab_gh5200.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import validate_crontab as vc  # noqa: E402

GOOD_CRONTAB = """\
FLEET_ENV_FILE=/fleet-kit/fleet.env
PATH=/root/.local/bin:/usr/bin:/bin
HOME=/root
FLEET_SHARE_DIR=/fleet-kit/shares

*/10 * * * * root echo hi
"""

EMPTY_ASSIGNMENT_CRONTAB = """\
FLEET_ENV_FILE=/fleet-kit/fleet.env
PATH=/root/.local/bin:/usr/bin:/bin
HOME=/root
FLEET_SHARE_DIR=
FLEET_LEASE_DIR=
FLEET_INSTANCE_NAME=

*/10 * * * * root echo hi
"""


def _write(text):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".cron", delete=False)
    f.write(text)
    f.close()
    return f.name


class EmptyEnvAssignmentTests(unittest.TestCase):
    def test_clean_file_with_populated_env_still_passes(self):
        path = _write(GOOD_CRONTAB)
        _lines, bad = vc.check(path)
        self.assertEqual(bad, [])

    def test_empty_env_assignment_is_flagged(self):
        path = _write(EMPTY_ASSIGNMENT_CRONTAB)
        _lines, bad = vc.check(path)
        flagged_lines = {raw.strip() for _ln, raw, _why in bad}
        self.assertIn("FLEET_SHARE_DIR=", flagged_lines)
        self.assertIn("FLEET_LEASE_DIR=", flagged_lines)
        self.assertIn("FLEET_INSTANCE_NAME=", flagged_lines)
        for _ln, _raw, why in bad:
            self.assertIn("empty env assignment", why)

    def test_fix_quarantines_only_the_empty_lines_not_the_whole_file(self):
        path = _write(EMPTY_ASSIGNMENT_CRONTAB)
        lines, bad = vc.check(path)
        self.assertEqual(len(bad), 3)
        for ln, _raw, why in bad:
            lines[ln - 1] = "# QUARANTINED by validate_crontab.py (%s): %s" % (why, lines[ln - 1])
        fixed = "\n".join(lines)
        self.assertIn("*/10 * * * * root echo hi", fixed)
        self.assertIn("# QUARANTINED", fixed)


if __name__ == "__main__":
    unittest.main()
