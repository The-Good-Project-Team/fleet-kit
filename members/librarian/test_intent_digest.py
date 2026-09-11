#!/usr/bin/env python3
"""test_intent_digest.py -- proves gh#896's fix: from_gh() must read every page of the
(oldest-first, 100-per-page) GitHub issues/comments feed, not just the first, since the newest
`Reif:` comment on an active repo otherwise lives on a later page it never reaches."""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intent_digest as idg  # noqa: E402


def _page(rows, next_url=None):
    headers = {}
    if next_url:
        headers["link"] = f'<{next_url}>; rel="next", <https://api.github.com/last>; rel="last"'
    return headers, json.dumps(rows)


def _comment(body, when):
    return {"body": body, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))}


class FromGhPaginationTest(unittest.TestCase):
    def test_reif_comment_on_last_of_three_pages_is_returned(self):
        """AC1: a 3-page feed (100, 100, 40) with the Reif: comment only on the last page."""
        now = time.time()
        page1 = [_comment("filler", now) for _ in range(100)]
        page2 = [_comment("filler", now) for _ in range(100)]
        page3 = [_comment("filler", now) for _ in range(39)] + [_comment("Reif: ship it", now)]
        responses = [
            _page(page1, next_url="repos/o/r/issues/comments?page=2"),
            _page(page2, next_url="repos/o/r/issues/comments?page=3"),
            _page(page3, next_url=None),
        ]
        calls = []

        def fake_run(endpoint, timeout):
            calls.append(endpoint)
            return responses[len(calls) - 1]

        rows, note = idg.from_gh(["o/r"], now - 3600, run=fake_run)
        texts = [r["text"] for r in rows]
        self.assertIn("Reif: ship it", texts)
        self.assertEqual(len(calls), 3)
        self.assertIn("o/r: ok (3 pages, 1 rows)", note)

    def test_follows_link_header_until_exhausted(self):
        """AC2 (follow side): three linked pages all get fetched, in order."""
        now = time.time()
        responses = [
            _page([_comment("Reif: one", now)], next_url="page=2"),
            _page([_comment("Reif: two", now)], next_url="page=3"),
            _page([_comment("Reif: three", now)], next_url=None),
        ]
        calls = []

        def fake_run(endpoint, timeout):
            calls.append(endpoint)
            return responses[len(calls) - 1]

        rows, note = idg.from_gh(["o/r"], now - 3600, run=fake_run)
        self.assertEqual(len(calls), 3)
        self.assertEqual({r["text"] for r in rows}, {"Reif: one", "Reif: two", "Reif: three"})
        self.assertEqual(calls[1], "page=2")
        self.assertEqual(calls[2], "page=3")

    def test_no_link_header_means_exactly_one_request(self):
        """AC2 (stop side): a short single-page feed with no Link header issues one call."""
        now = time.time()
        calls = []

        def fake_run(endpoint, timeout):
            calls.append(endpoint)
            return _page([_comment("Reif: only", now)], next_url=None)

        rows, note = idg.from_gh(["o/r"], now - 3600, run=fake_run)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r["text"] for r in rows], ["Reif: only"])

    def test_note_reports_page_and_row_count_for_a_short_single_page_feed(self):
        """AC3: the note names the page count read, so a thin digest can be told apart from
        a truncated one from the note string alone."""
        now = time.time()
        rows_in = [_comment("Reif: a", now) for _ in range(12)]

        def fake_run(endpoint, timeout):
            return _page(rows_in, next_url=None)

        rows, note = idg.from_gh(["o/r"], now - 3600, run=fake_run)
        self.assertIn("o/r: ok (1 page, 12 rows)", note)

    def test_partial_failure_on_a_later_page_is_named_not_reported_ok(self):
        """AC4: page 2 of 3 fails -- rows already collected are kept, and the note says
        partial instead of the old fail-open plain 'ok'."""
        now = time.time()

        def fake_run(endpoint, timeout):
            if fake_run.calls == 0:
                fake_run.calls += 1
                return _page([_comment("Reif: kept", now)], next_url="page=2")
            raise RuntimeError("boom")

        fake_run.calls = 0
        rows, note = idg.from_gh(["o/r"], now - 3600, run=fake_run)
        self.assertEqual([r["text"] for r in rows], ["Reif: kept"])
        self.assertIn("o/r: partial (1 of >=2 pages, RuntimeError)", note)
        self.assertNotIn("o/r: ok", note)

    def test_no_gh_repo_note_unchanged(self):
        rows, note = idg.from_gh([], time.time() - 3600)
        self.assertEqual(rows, [])
        self.assertEqual(note, "no --gh-repo")


class HeaderParsingTest(unittest.TestCase):
    def test_split_headers_body_parses_curl_style_output(self):
        raw = ('HTTP/2.0 200 OK\r\n'
               'link: <https://api.github.com/x?page=2>; rel="next"\r\n'
               '\r\n'
               '[{"a": 1}]')
        headers, body = idg._split_headers_body(raw)
        self.assertEqual(headers["link"], '<https://api.github.com/x?page=2>; rel="next"')
        self.assertEqual(json.loads(body), [{"a": 1}])

    def test_next_page_url_absent_returns_none(self):
        headers = {"link": '<https://api.github.com/x?page=1>; rel="last"'}
        self.assertIsNone(idg._next_page_url(headers))

    def test_next_page_url_present_extracts_url(self):
        headers = {"link": '<https://api.github.com/x?page=2>; rel="next", '
                            '<https://api.github.com/x?page=5>; rel="last"'}
        self.assertEqual(idg._next_page_url(headers), "https://api.github.com/x?page=2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
