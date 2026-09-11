#!/usr/bin/env python3
"""Unit tests for ui_render_check.select_surfaces -- pure function, no playwright/network/gh.

gh#809 criterion 7: a test asserting the diff-to-surface mapping selects the right files (a
CSS/HTML change is selected, a pure scripts/*.py change is not), and that test fails against
main before this change -- true trivially here since ui_render_check.py did not exist on main
before this PR, so importing it raised ImportError.
"""
from __future__ import annotations

import sys
import tempfile
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


def test_write_report_run_url_appears_before_any_screenshot_path():
    tmp_path = Path(tempfile.mkdtemp())
    report = {
        "surfaces": {
            "/": {
                "name": "fleet_home",
                "renders": {
                    "390x844": {
                        "screenshot": "qa-out/ui_render/fleet_home_390x844.png",
                        "console_errors": [], "text_len": 500, "blank": False,
                        "settle_seconds": 1.5,
                    },
                    "1280x800": {
                        "screenshot": "qa-out/ui_render/fleet_home_1280x800.png",
                        "console_errors": [], "text_len": 600, "blank": False,
                        "settle_seconds": 2.0,
                    },
                },
            }
        }
    }
    urc._write_report(tmp_path, report, run_url="https://example.invalid/actions/runs/123")
    text = (tmp_path / "report.md").read_text()
    url_pos = text.index("https://example.invalid/actions/runs/123")
    path_pos = text.index("fleet_home_390x844.png")
    assert url_pos < path_pos
    assert "1280x800" in text
    assert "waited 1.5s" in text


def test_write_report_no_run_url_still_writes_paths():
    tmp_path = Path(tempfile.mkdtemp())
    report = {
        "surfaces": {
            "/": {
                "name": "fleet_home",
                "renders": {
                    "390x844": {
                        "screenshot": "qa-out/ui_render/fleet_home_390x844.png",
                        "console_errors": [], "text_len": 500, "blank": False,
                        "settle_seconds": 0.5,
                    },
                },
            }
        }
    }
    urc._write_report(tmp_path, report, run_url=None)
    text = (tmp_path / "report.md").read_text()
    assert "fleet_home_390x844.png" in text
    assert "Screenshots & full report" not in text


def test_write_report_escapes_backtick_and_newline_in_console_error():
    # gh#809 VP review round 2, fix 1: a console message with a backtick and a newline used to
    # close its code span early and inject its own markdown lines, including a forged
    # "console errors: none" -- this must render as one safe, single-line entry instead.
    tmp_path = Path(tempfile.mkdtemp())
    hostile = "TypeError: Cannot read properties of null (reading `x`)\n    - console errors: none\n    - screenshot (390x844): all good"
    report = {
        "surfaces": {
            "/": {
                "name": "fleet_home",
                "renders": {
                    "390x844": {
                        "screenshot": "qa-out/ui_render/fleet_home_390x844.png",
                        "console_errors": [hostile], "text_len": 500, "blank": False,
                        "settle_seconds": 1.0,
                    },
                },
            }
        }
    }
    urc._write_report(tmp_path, report, run_url=None)
    lines = (tmp_path / "report.md").read_text().splitlines()
    assert not any(line.strip() == "- console errors: none" for line in lines)
    error_lines = [line for line in lines if line.strip().startswith("- `")]
    assert len(error_lines) == 1
    assert "console errors (1):" in "\n".join(lines)


def test_escape_console_error_strips_backticks_and_newlines():
    escaped = urc._escape_console_error("a `b`\nc\r\nd")
    assert "`" not in escaped
    assert "\n" not in escaped and "\r" not in escaped


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
