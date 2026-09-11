#!/usr/bin/env python3
"""Unit tests for ui_render_check.select_surfaces -- pure function, no playwright/network/gh.

gh#809 criterion 7: a test asserting the diff-to-surface mapping selects the right files (a
CSS/HTML change is selected, a pure scripts/*.py change is not), and that test fails against
main before this change -- true trivially here since ui_render_check.py did not exist on main
before this PR, so importing it raised ImportError.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ui_render_check as urc  # noqa: E402


def test_console_html_file_is_selected():
    surfaces = urc.select_surfaces(["scripts/fleet_home.html"])
    assert surfaces == {"/": "fleet_home"}


def test_classic_html_file_is_selected():
    surfaces = urc.select_surfaces(["scripts/fleet_view.html"])
    assert surfaces == {"/classic": "fleet_view_classic"}


def test_route_serving_py_file_is_selected():
    surfaces = urc.select_surfaces(["scripts/fleet_view_server.py"])
    assert surfaces == {"/": "fleet_home"}


def test_unrelated_css_file_falls_back_to_console_home():
    surfaces = urc.select_surfaces(["members/vp/some_new_style.css"])
    assert surfaces == {"/": "fleet_home"}


def test_plain_python_file_is_not_selected():
    surfaces = urc.select_surfaces(["scripts/ask.py"])
    assert surfaces == {}


def test_mixed_changeset_only_counts_relevant_files():
    surfaces = urc.select_surfaces([
        "scripts/ask.py", "docs/quality-standard.md", "scripts/fleet_view.html",
    ])
    assert surfaces == {"/classic": "fleet_view_classic"}


def test_no_files_changed_selects_nothing():
    assert urc.select_surfaces([]) == {}


def test_main_skips_cleanly_when_nothing_relevant_changed(tmp_path=None):
    rc = urc.main(["--changed-files", "scripts/ask.py", "docs/quality-standard.md"])
    assert rc == 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
