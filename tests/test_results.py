# tests/test_results.py
"""Aggregation: parallel reads, per-profile failures, and one row per URL."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin"))

import results  # noqa: E402


class FakeItem:
    """Stands in for `browsers.HistoryItem`, which only needs a URL and a time."""

    def __init__(self, url, visited_at):
        self.url = url
        self._visited_at = visited_at

    def normalized_time(self):
        return self._visited_at

    def __eq__(self, other):
        return (self.url, self._visited_at) == (other.url, other._visited_at)

    def __repr__(self):
        return f"FakeItem({self.url!r}, {self._visited_at})"


class FakeBrowser:
    """Stands in for `browsers.Browser` and records how it was queried."""

    def __init__(self, name, items=None, error=None):
        self.name = name
        self.items = items or []
        self.error = error
        self.calls = []

    def history(self, search_term="", limit=100, blocked_domains=None):
        self.calls.append((search_term, limit, list(blocked_domains or ())))
        if self.error:
            raise self.error
        return list(self.items)


class NewestUniqueTests(unittest.TestCase):
    def test_keeps_the_most_recent_row_for_a_repeated_url(self):
        older = FakeItem("https://example.com/page", 1_000)
        newer = FakeItem("https://example.com/page", 2_000)
        other = FakeItem("https://example.com/other", 1_500)

        self.assertEqual(results.newest_unique([older, other, newer], 10), [newer, other])

    def test_orders_newest_first_and_truncates(self):
        items = [FakeItem(f"https://example.com/{index}", index) for index in range(10)]
        ordered = results.newest_unique(items, 3)
        self.assertEqual([item.url for item in ordered], [
            "https://example.com/9",
            "https://example.com/8",
            "https://example.com/7",
        ])

    def test_empty_input(self):
        self.assertEqual(results.newest_unique([], 5), [])


class ReadProfilesTests(unittest.TestCase):
    def test_single_profile_does_not_start_a_thread_pool(self):
        browser = FakeBrowser("brave", [FakeItem("https://example.com", 1)])
        items, warnings = results.read_profiles([browser], "term", 25, ["blocked.com"])

        self.assertEqual(warnings, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(browser.calls, [("term", 25, ["blocked.com"])])

    def test_reads_every_profile_in_parallel(self):
        browsers = [
            FakeBrowser("brave", [FakeItem(f"https://example.com/{index}", index)])
            for index in range(5)
        ]
        items, warnings = results.read_profiles(browsers, "term", 10)

        self.assertEqual(warnings, [])
        self.assertEqual(len(items), 5)
        self.assertEqual(len({item.url for item in items}), 5)

    def test_a_broken_profile_warns_instead_of_hiding_the_others(self):
        browsers = [
            FakeBrowser("brave", [FakeItem("https://example.com/good", 1)]),
            FakeBrowser("edge", error=OSError("database is locked")),
        ]
        items, warnings = results.read_profiles(browsers, "term", 10)

        self.assertEqual([item.url for item in items], ["https://example.com/good"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("edge", warnings[0])
        self.assertIn("database is locked", warnings[0])

    def test_a_profile_that_vanished_is_skipped_silently(self):
        browsers = [
            FakeBrowser("brave", [FakeItem("https://example.com/good", 1)]),
            FakeBrowser("edge", error=FileNotFoundError("gone")),
        ]
        items, warnings = results.read_profiles(browsers, "term", 10)

        self.assertEqual(len(items), 1)
        self.assertEqual(warnings, [])

    def test_no_profiles(self):
        self.assertEqual(results.read_profiles([], "term", 10), ([], []))

    def test_results_are_merged_one_row_per_url(self):
        browsers = [
            FakeBrowser("brave", [FakeItem("https://example.com/shared", 100)]),
            FakeBrowser("edge", [FakeItem("https://example.com/shared", 200)]),
        ]
        items, _ = results.read_profiles(browsers, "term", 10)
        merged = results.newest_unique(items, 10)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].normalized_time(), 200)


if __name__ == "__main__":
    unittest.main()
