"""gh#818: a member's CLAUDE_CONFIG_DIR must be writable or the whole fleet is amnesiac.

Found live 2026-09-11: up.sh mounted each account's dir `:ro` while deploy.sh mounted the
same path read-write. Under `:ro` the Claude CLI silently cannot write
$CLAUDE_CONFIG_DIR/projects/*/memory (every member's memory) and TaskCreate/TodoWrite ENOENT
on every call -- in charters whose step 1 is "call TodoWrite". Nothing failed loudly; members
just re-derived the same context every pass and said so in their self-critiques.

These tests pin the two things that must hold forever:
  - when the real dir IS writable, nothing changes at all (no mirror, no surprises on the
    auth path that every single pass in the fleet runs through);
  - when it is not, a writable mirror is produced whose credentials are SYMLINKS to the real
    dir, so the host stays the single source of truth for auth and token rotation needs no
    sync step.
"""
import os
import subprocess
import tempfile
import unittest

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_pool.sh")


def run_helper(home, log_dir, *, force_unwritable, account="acct"):
    """Source account_pool.sh and call _account_pool_config_dir for `account`.

    Root ignores permission bits, so a genuinely read-only directory cannot be created in a
    unit test -- instead we redefine the one function that answers "is this writable?", which
    is exactly why it was split out.
    """
    override = (
        "_account_pool_dir_writable() { return 1; }\n" if force_unwritable else ""
    )
    script = (
        f'set -u\n. "{POOL}"\n{override}'
        f'_account_pool_config_dir "{account}"\n'
    )
    env = dict(os.environ, HOME=home, FLEET_LOG_DIR=log_dir)
    env.pop("FLEET_CLAUDE_STATE_DIR", None)
    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    # _account_pool_log writes to stderr/logfile; the resolved path is the last stdout line.
    return out.stdout.strip().splitlines()[-1]


class ConfigDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.logs = os.path.join(self.tmp.name, "logs")
        self.real = os.path.join(self.home, ".claude-acct")
        os.makedirs(self.real)
        os.makedirs(self.logs)
        with open(os.path.join(self.real, ".credentials.json"), "w") as fh:
            fh.write('{"token": "real"}')
        with open(os.path.join(self.real, "settings.json"), "w") as fh:
            fh.write("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writable_dir_is_used_verbatim(self):
        """The common case (deploy.sh's rw mount) must not change behaviour at all."""
        self.assertEqual(run_helper(self.home, self.logs, force_unwritable=False), self.real)
        self.assertFalse(os.path.exists(os.path.join(self.logs, "claude-state")))

    def test_readonly_dir_falls_back_to_a_writable_mirror(self):
        got = run_helper(self.home, self.logs, force_unwritable=True)
        self.assertEqual(got, os.path.join(self.logs, "claude-state", "acct"))
        # The three dirs the CLI must write, and the reason this exists at all.
        for sub in ("projects", "todos", "tasks"):
            path = os.path.join(got, sub)
            self.assertTrue(os.path.isdir(path), f"{sub} missing")
            probe = os.path.join(path, "probe")
            with open(probe, "w") as fh:
                fh.write("x")   # would raise EROFS/ENOENT before this fix
            self.assertTrue(os.path.exists(probe))

    def test_mirror_symlinks_credentials_rather_than_copying_them(self):
        """Auth must keep reading the HOST's current file, so token rotation needs no sync."""
        got = run_helper(self.home, self.logs, force_unwritable=True)
        creds = os.path.join(got, ".credentials.json")
        self.assertTrue(os.path.islink(creds), ".credentials.json must be a symlink, not a copy")
        self.assertEqual(
            os.path.realpath(creds),
            os.path.realpath(os.path.join(self.real, ".credentials.json")),
        )
        # Rotating the host file is visible immediately through the mirror.
        with open(os.path.join(self.real, ".credentials.json"), "w") as fh:
            fh.write('{"token": "rotated"}')
        with open(creds) as fh:
            self.assertIn("rotated", fh.read())

    def test_mirror_preserves_preexisting_project_history(self):
        """fleet-code-review BLOCK on this PR: a project dir that already existed under the
        read-only $real (e.g. seeded before the :ro mount landed) must not become invisible
        just because it now routes through the mirror -- that's the exact amnesia this PR
        exists to cure, reintroduced via a different mechanism."""
        old_memory = os.path.join(self.real, "projects", "-repo", "memory")
        os.makedirs(old_memory)
        with open(os.path.join(old_memory, "MEMORY.md"), "w") as fh:
            fh.write("- [Old thing](old.md)")

        got = run_helper(self.home, self.logs, force_unwritable=True)

        mirrored = os.path.join(got, "projects", "-repo", "memory", "MEMORY.md")
        self.assertTrue(os.path.exists(mirrored), "pre-existing project history was dropped")
        with open(mirrored) as fh:
            self.assertEqual(fh.read(), "- [Old thing](old.md)")

        # A fresh write alongside the preserved history must still work (mirror stays writable).
        new_file = os.path.join(got, "projects", "-repo", "memory", "NEW.md")
        with open(new_file, "w") as fh:
            fh.write("new")
        self.assertTrue(os.path.exists(new_file))

    def test_mirror_is_idempotent_and_preserves_written_state(self):
        """Every pass calls this; a second call must not wipe the memory the first one wrote."""
        got = run_helper(self.home, self.logs, force_unwritable=True)
        memory = os.path.join(got, "projects", "-repo", "memory")
        os.makedirs(memory)
        with open(os.path.join(memory, "MEMORY.md"), "w") as fh:
            fh.write("- [Thing](thing.md)")
        again = run_helper(self.home, self.logs, force_unwritable=True)
        self.assertEqual(again, got)
        with open(os.path.join(memory, "MEMORY.md")) as fh:
            self.assertEqual(fh.read(), "- [Thing](thing.md)")


if __name__ == "__main__":
    unittest.main()
