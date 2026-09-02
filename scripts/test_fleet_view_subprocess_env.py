"""A child process this server spawns must see fleet.env's values.

RED without subprocess_env(): the child inherits a process environment that never carried
FLEET_MAXX_* (the container is handed FLEET_ENV_FILE, a path, not the file's values), so
maxx_share_ceiling.py reads an unconfigured meter and the Settings page reports the meter
as unreadable while it is healthy.
"""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent


class SubprocessEnvCarriesFleetEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.tmp.write("FLEET_MAXX_URL=https://example.invalid\n"
                       "FLEET_MAXX_HANDLE=probe\n"
                       "FLEET_MAXX_KEY=probe-secret\n")
        self.tmp.close()
        os.environ["FLEET_ENV_FILE"] = self.tmp.name
        # The container state this reproduces: the values exist ONLY in the file.
        for k in ("FLEET_MAXX_URL", "FLEET_MAXX_HANDLE", "FLEET_MAXX_KEY"):
            os.environ.pop(k, None)
        sys.path.insert(0, str(KIT / "scripts"))
        for mod in [m for m in list(sys.modules) if m == "fleet_view_server"]:
            del sys.modules[mod]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_child_process_sees_maxx_credentials_from_the_file(self):
        import fleet_view_server as fvs
        env = fvs.subprocess_env()
        for k, want in (("FLEET_MAXX_URL", "https://example.invalid"),
                        ("FLEET_MAXX_HANDLE", "probe"),
                        ("FLEET_MAXX_KEY", "probe-secret")):
            self.assertEqual(env.get(k), want,
                             f"{k} must reach a child process from fleet.env, not os.environ")

        # And prove it end to end: a child launched with that env actually receives them.
        out = subprocess.run(
            [sys.executable, "-c",
             "import os;print(os.environ.get('FLEET_MAXX_HANDLE',''))"],
            capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(out.stdout.strip(), "probe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
