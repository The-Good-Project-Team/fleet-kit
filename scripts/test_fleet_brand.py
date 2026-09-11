"""Regression test for gh#553 fix 5.

Every fleet-kit instance's console used to show the tab title and topbar brand as the literal
string "fleet-kit", regardless of which product's console it actually was -- fleet_view.html:6
and :316 hardcoded it. `read_env_flags()`'s BRAND field now comes from FLEET_BRAND when an
operator sets it, else a name derived from the instance's own REPO_URL, else the generic
"Fleet" -- never the old fixed string.

Run: python3 scripts/test_fleet_brand.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "scripts"))

import fleet_view_server as fvs  # noqa: E402


def _reimport_with_env_file(path: str):
    """ENV_FILE is resolved once at import time from FLEET_ENV_FILE, so a test that wants
    read_env_values() to see a temp file's contents must reimport the module after setting
    the env var -- setting it after fvs is already loaded is silently ignored."""
    os.environ["FLEET_ENV_FILE"] = path
    sys.modules.pop("fleet_view_server", None)
    import fleet_view_server as reloaded  # noqa: PLC0415
    return reloaded


class BrandFromRepoUrl(unittest.TestCase):
    def test_empty_url_yields_empty_string(self):
        self.assertEqual(fvs._brand_from_repo_url(""), "")

    def test_hyphenated_slug_becomes_title_case_words(self):
        self.assertEqual(
            fvs._brand_from_repo_url("https://github.com/org/philanthropy-atlas"),
            "Philanthropy Atlas")

    def test_underscored_slug_becomes_title_case_words(self):
        self.assertEqual(
            fvs._brand_from_repo_url("https://github.com/org/fleet_kit_server"),
            "Fleet Kit Server")

    def test_single_word_slug(self):
        self.assertEqual(fvs._brand_from_repo_url("https://github.com/org/atlas"), "Atlas")

    def test_trailing_slash_does_not_produce_an_empty_last_segment(self):
        self.assertEqual(
            fvs._brand_from_repo_url("https://github.com/org/philanthropy-atlas/"),
            "Philanthropy Atlas")


class ReadEnvFlagsBrand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.tmp.close()
        self._old_env_file = os.environ.get("FLEET_ENV_FILE")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_env_file is None:
            os.environ.pop("FLEET_ENV_FILE", None)
        else:
            os.environ["FLEET_ENV_FILE"] = self._old_env_file
        os.unlink(self.tmp.name)
        sys.modules.pop("fleet_view_server", None)

    def _write_env(self, text: str) -> None:
        Path(self.tmp.name).write_text(text)

    def test_explicit_flag_brand_wins_over_repo_derivation(self):
        self._write_env("FLEET_BRAND=Philanthropy Atlas\n")
        mod = _reimport_with_env_file(self.tmp.name)
        with unittest.mock.patch.object(mod, "REPO_URL", "https://github.com/org/some-other-repo"):
            out = mod.read_env_flags()
        self.assertEqual(out["BRAND"], "Philanthropy Atlas")

    def test_falls_back_to_repo_derived_name_when_unset(self):
        self._write_env("")
        mod = _reimport_with_env_file(self.tmp.name)
        with unittest.mock.patch.object(mod, "REPO_URL", "https://github.com/org/philanthropy-atlas"):
            out = mod.read_env_flags()
        self.assertEqual(out["BRAND"], "Philanthropy Atlas")

    def test_falls_back_to_generic_fleet_when_nothing_resolves(self):
        self._write_env("")
        mod = _reimport_with_env_file(self.tmp.name)
        with unittest.mock.patch.object(mod, "REPO_URL", ""):
            out = mod.read_env_flags()
        self.assertEqual(out["BRAND"], "Fleet")

    def test_blank_flag_value_is_treated_as_unset(self):
        self._write_env("FLEET_BRAND=\n")
        mod = _reimport_with_env_file(self.tmp.name)
        with unittest.mock.patch.object(mod, "REPO_URL", "https://github.com/org/philanthropy-atlas"):
            out = mod.read_env_flags()
        self.assertEqual(out["BRAND"], "Philanthropy Atlas")


if __name__ == "__main__":
    unittest.main()
