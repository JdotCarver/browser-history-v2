# tests/test_plugin_main.py
"""The Flow Launcher glue: how many rows each profile is asked for, and what is shown."""

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import flow_stub  # noqa: E402

flow_stub.install()

import sqlite3  # noqa: E402  (imported for the failure test)
import formatting  # noqa: E402
import main  # noqa: E402


class FakeItem:
    """Stands in for `browsers.HistoryItem` where `main` only needs to display it."""

    def __init__(self, title, url, visited_at):
        self.title = title
        self.url = url
        self._visited_at = visited_at

    def timestamp(self):
        return datetime.datetime(2024, 1, 1, 12, 0, 0)

    def normalized_time(self):
        return self._visited_at


def make_plugin(history_limit=1000, max_ui_results=500, browsers=None):
    """A plugin instance with the settings that `__init__` would have read.

    `_ready` is set because these tests render results directly: discovery is
    what a real query does first, and it is tested on its own below.
    """
    plugin = object.__new__(main.BrowserHistory)
    plugin._ready = True
    plugin.browsers = browsers if browsers is not None else [object()]
    plugin.history_limit = history_limit
    plugin.max_ui_results = max_ui_results
    plugin.blocked_domains = []
    plugin._warnings = []
    plugin.items = []
    return plugin


class LazyDiscoveryTests(unittest.TestCase):
    """Discovery belongs to the query path: the context menu must not pay for it."""

    def setUp(self):
        # No chdir: on Windows a temp directory cannot be removed while it is
        # the working directory, and construction touches nothing anyway. The
        # cache is redirected so a query never writes into the repository.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name) / "Cache"

    def make_plugin(self):
        plugin = main.BrowserHistory()
        plugin.cache_dir = self.cache_dir
        plugin.favicons.directory = self.cache_dir / "favicons"
        return plugin

    def test_constructing_the_plugin_explores_nothing(self):
        with mock.patch.object(main.browsers, "get", side_effect=AssertionError("discovery")), \
                mock.patch.object(main.browsers, "get_all_profiles", side_effect=AssertionError("discovery")):
            plugin = self.make_plugin()

        self.assertEqual(plugin.browsers, [])
        self.assertFalse(plugin._ready)
        self.assertFalse(self.cache_dir.exists(), "construction must not touch the disk")

    def test_the_context_menu_is_built_without_browsers(self):
        plugin = self.make_plugin()

        plugin.context_menu(["A title", "https://example.com/x"])

        self.assertEqual(
            [item["title"] for item in plugin.items],
            ["Open in browser", "Copy to clipboard"],
        )

    def test_the_first_query_discovers_and_the_second_does_not(self):
        plugin = self.make_plugin()
        calls = []

        def counting(*args, **kwargs):
            calls.append(args)
            return None

        with mock.patch.object(main.browsers, "get", side_effect=counting):
            plugin.query("")
            after_first = len(calls)
            plugin.query("")

        self.assertGreater(after_first, 0)
        self.assertEqual(len(calls), after_first, "discovery must not repeat per query")

    def test_a_query_with_no_browsers_still_explains_itself(self):
        plugin = self.make_plugin()

        with mock.patch.object(main.browsers, "get", return_value=None):
            plugin.query("")

        self.assertEqual(plugin.items[0]["title"], "Browser not available")


class ClipboardTests(unittest.TestCase):
    def test_copying_works_with_the_import_deferred(self):
        _flox, pyperclip = flow_stub.install()
        pyperclip.copied.clear()
        plugin = make_plugin()

        plugin.copy_to_clipboard("https://example.com/x")

        self.assertEqual(pyperclip.copied, ["https://example.com/x"])

    def test_the_module_does_not_import_pyperclip_on_load(self):
        """Loading the clipboard module costs time on every keystroke, including menus."""
        self.assertFalse(hasattr(main, "pyperclip"))


class RowBudgetTests(unittest.TestCase):
    def read_with_budget(self, history_limit, max_ui_results):
        captured = {}

        def fake_read_profiles(browsers, search_term, limit, blocked_domains):
            captured["limit"] = limit
            captured["search_term"] = search_term
            return [], []

        original = main.read_profiles
        main.read_profiles = fake_read_profiles
        try:
            make_plugin(history_limit, max_ui_results)._get_combined_history("a term")
        finally:
            main.read_profiles = original
        return captured

    def test_budget_is_capped_by_what_the_ui_can_show(self):
        """Reading 1000 rows per profile for a 500-row display wastes work."""
        self.assertEqual(self.read_with_budget(1000, 500)["limit"], 500)

    def test_a_lower_history_limit_still_wins(self):
        self.assertEqual(self.read_with_budget(200, 500)["limit"], 200)

    def test_equal_limits_are_unchanged(self):
        self.assertEqual(self.read_with_budget(300, 300)["limit"], 300)

    def test_search_term_reaches_the_profiles(self):
        self.assertEqual(self.read_with_budget(1000, 500)["search_term"], "a term")


class CombinedHistoryTests(unittest.TestCase):
    def test_newest_row_wins_when_profiles_disagree_about_a_url(self):
        """Two browsers can both know a URL; the more recent visit must be shown."""
        items = [
            FakeItem("Newer", "https://example.com/shared", 300),
            FakeItem("Older", "https://example.com/shared", 100),
            FakeItem("Other", "https://example.com/other", 200),
        ]

        original = main.read_profiles
        main.read_profiles = lambda *args, **kwargs: (items, [])
        try:
            combined = make_plugin()._get_combined_history("term")
        finally:
            main.read_profiles = original

        self.assertEqual([item.title for item in combined], ["Newer", "Other"])

    def test_combined_history_is_capped_by_the_ui_limit(self):
        items = [FakeItem(f"Item {index}", f"https://example.com/{index}", index)
                 for index in range(20)]

        original = main.read_profiles
        main.read_profiles = lambda *args, **kwargs: (items, [])
        try:
            combined = make_plugin(max_ui_results=5)._get_combined_history("term")
        finally:
            main.read_profiles = original

        self.assertEqual(len(combined), 5)


class QueryRenderingTests(unittest.TestCase):
    def test_results_are_rendered_with_their_timestamp_and_url(self):
        plugin = make_plugin()
        plugin._get_combined_history = lambda query: [
            FakeItem("Shared", "https://example.com/shared", 300),
            FakeItem("Other", "https://example.com/other", 200),
        ]

        plugin.query("term")

        self.assertEqual([item["title"] for item in plugin.items], ["Shared", "Other"])
        self.assertIn("https://example.com/shared", plugin.items[0]["subtitle"])
        self.assertIn("Jan 01, 2024", plugin.items[0]["subtitle"])
        self.assertEqual(plugin.items[0]["parameters"], ["https://example.com/shared"])

    def test_display_settings_reach_the_subtitle(self):
        """The four display settings are wired through, not just defined."""
        plugin = make_plugin()
        plugin._get_combined_history = lambda query: [
            FakeItem("Shared", "https://www.example.com/shared?x=1", 300)
        ]
        plugin.date_display = formatting.DATE_RELATIVE
        plugin.time_format = formatting.TIME_24
        plugin.url_display = formatting.URL_DOMAIN
        plugin.subtitle_layout = formatting.LAYOUT_DASHED

        plugin.query("term")

        # Dated 2024, so the relative form is the plain date.
        self.assertEqual(
            plugin.items[0]["subtitle"], "Jan 01, 2024 - example.com"
        )

    def test_favicons_are_off_unless_asked_for(self):
        """The default costs nothing: the icon store is never consulted."""
        plugin = make_plugin()
        plugin._get_combined_history = lambda query: [
            FakeItem("Shared", "https://example.com/shared", 300)
        ]

        class UnusedStore:
            def resolve(self, items):
                raise AssertionError("favicons must not be looked up when off")

        plugin.favicons = UnusedStore()
        plugin.query("term")

        self.assertEqual(plugin.items[0]["title"], "Shared")

    def test_an_icon_replaces_the_glyph(self):
        plugin = make_plugin()
        plugin._get_combined_history = lambda query: [
            FakeItem("Shared", "https://example.com/shared", 300)
        ]

        class Store:
            def resolve(self, items):
                return {items[0].url: "C:/icons/abc.png"}

        plugin.show_favicons = True
        plugin.favicons = Store()
        plugin.query("term")

        self.assertEqual(plugin.items[0]["icon"], "C:/icons/abc.png")
        self.assertIsNone(plugin.items[0]["glyph"])

    def test_a_url_without_an_icon_keeps_the_glyph(self):
        plugin = make_plugin()
        plugin._get_combined_history = lambda query: [
            FakeItem("Shared", "https://example.com/shared", 300)
        ]

        class Store:
            def resolve(self, items):
                return {}

        plugin.show_favicons = True
        plugin.favicons = Store()
        plugin.query("term")

        self.assertEqual(plugin.items[0]["glyph"], main.HISTORY_GLYPH)

    def test_a_broken_icon_lookup_never_costs_the_results(self):
        plugin = make_plugin()
        plugin._get_combined_history = lambda query: [
            FakeItem("Shared", "https://example.com/shared", 300)
        ]

        class Exploding:
            def resolve(self, items):
                raise sqlite3.OperationalError("database is locked")

        plugin.show_favicons = True
        plugin.favicons = Exploding()
        plugin.query("term")

        self.assertEqual([item["title"] for item in plugin.items], ["Shared"])

    def test_skipped_profiles_are_reported_above_the_results(self):
        plugin = make_plugin()

        def combined(query):
            plugin._warnings = ["edge: database is locked"]
            return [FakeItem("Result", "https://example.com/", 1)]

        plugin._get_combined_history = combined
        plugin.query("term")

        self.assertEqual(len(plugin.items), 2)
        self.assertEqual(plugin.items[0]["title"], "Some browsers or profiles were skipped")
        self.assertIn("database is locked", plugin.items[0]["subtitle"])
        self.assertEqual(plugin.items[1]["title"], "Result")

    def test_no_browsers_shows_an_explanation(self):
        plugin = make_plugin(browsers=[])
        plugin.init_error = "Could not locate any supported browser profile databases."

        plugin.query("term")

        self.assertEqual(len(plugin.items), 1)
        self.assertEqual(plugin.items[0]["title"], "Browser not available")

    def test_a_failing_search_shows_the_error_instead_of_crashing(self):
        plugin = make_plugin()

        def exploding(query):
            raise RuntimeError("something broke")

        plugin._get_combined_history = exploding
        plugin.query("term")

        self.assertEqual(plugin.items[0]["title"], "An error occurred")
        self.assertIn("something broke", plugin.items[0]["subtitle"])


if __name__ == "__main__":
    unittest.main()
