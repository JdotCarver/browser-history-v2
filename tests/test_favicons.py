# tests/test_favicons.py
"""Icons from the browser's own store, and the cache in front of it."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from favicons import (  # noqa: E402
    MAX_INDEX_ENTRIES,
    FaviconStore,
    sniff_extension,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
ICO_HEADER = b"\x00\x00\x01\x00"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


def png(payload: bytes) -> bytes:
    return PNG_HEADER + payload


class FakeBrowser:
    """The two things the store needs: an engine, and an icon database copy."""

    def __init__(self, icons: Path, chromium: bool = True) -> None:
        self.timestamp_type = "chromium" if chromium else "unix_us"
        self._icons = icons
        self.copies = 0

    def copy_icons_database(self):
        self.copies += 1
        return str(self._icons)


class FakeItem:
    def __init__(self, url: str, browser=None) -> None:
        self.url = url
        self.browser = browser


def make_chromium_icons(path: Path, rows) -> Path:
    """rows: (page_url, image_data, width)."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE icon_mapping(id INTEGER PRIMARY KEY, page_url TEXT NOT NULL, icon_id INTEGER);
        CREATE TABLE favicons(id INTEGER PRIMARY KEY, url TEXT NOT NULL, icon_type INTEGER DEFAULT 1);
        CREATE TABLE favicon_bitmaps(
            id INTEGER PRIMARY KEY, icon_id INTEGER NOT NULL, last_updated INTEGER DEFAULT 0,
            image_data BLOB, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0
        );
        """
    )
    for index, (page_url, image_data, width) in enumerate(rows):
        connection.execute(
            "INSERT INTO favicons(id, url) VALUES (?, ?)", (index, "https://example.com/favicon.ico")
        )
        connection.execute(
            "INSERT INTO icon_mapping(page_url, icon_id) VALUES (?, ?)", (page_url, index)
        )
        connection.execute(
            "INSERT INTO favicon_bitmaps(icon_id, image_data, width, height) VALUES (?, ?, ?, ?)",
            (index, image_data, width, width),
        )
    connection.commit()
    connection.close()
    return path


def make_firefox_icons(path: Path, rows, with_to_pages: bool = True) -> Path:
    """rows: (page_url, data, width)."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE moz_pages_w_icons(id INTEGER PRIMARY KEY, page_url TEXT NOT NULL, page_url_hash INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE moz_icons(id INTEGER PRIMARY KEY, icon_url TEXT NOT NULL, width INTEGER DEFAULT 0, data BLOB);
        CREATE TABLE moz_icons_to_pages(page_id INTEGER, icon_id INTEGER);
        """
    )
    for index, (page_url, data, width) in enumerate(rows):
        connection.execute(
            "INSERT INTO moz_pages_w_icons(id, page_url) VALUES (?, ?)", (index, page_url)
        )
        connection.execute(
            "INSERT INTO moz_icons(id, icon_url, width, data) VALUES (?, ?, ?, ?)",
            (index, "https://example.com/favicon.ico", width, data),
        )
        if with_to_pages:
            connection.execute(
                "INSERT INTO moz_icons_to_pages(page_id, icon_id) VALUES (?, ?)", (index, index)
            )
    connection.commit()
    connection.close()
    return path


class TempCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache = self.root / "Cache"

    def tearDown(self):
        self._tmp.cleanup()

    def store(self, browsers=(), cache=None):
        return FaviconStore(cache or self.cache, browsers)


class Sniffing(unittest.TestCase):
    def test_recognised_formats(self):
        self.assertEqual(sniff_extension(png(b"x")), ".png")
        self.assertEqual(sniff_extension(ICO_HEADER + b"x"), ".ico")
        self.assertEqual(sniff_extension(b"GIF89a..."), ".gif")

    def test_anything_else_is_refused(self):
        self.assertIsNone(sniff_extension(SVG))
        self.assertIsNone(sniff_extension(b""))
        self.assertIsNone(sniff_extension(b"\x00\x01binary junk"))


class ChromiumIcons(TempCache):
    def test_an_exact_page_url_is_resolved_to_a_file(self):
        icons = make_chromium_icons(
            self.root / "Favicons", [("https://example.com/a", png(b"one"), 16)]
        )
        browser = FakeBrowser(icons)
        resolved = self.store([browser]).resolve(
            [FakeItem("https://example.com/a", browser)]
        )

        path = Path(resolved["https://example.com/a"])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), png(b"one"))
        self.assertEqual(path.suffix, ".png")

    def test_another_page_on_the_same_site_borrows_the_icon(self):
        """Chromium records an icon per visited URL, not per site."""
        icons = make_chromium_icons(
            self.root / "Favicons", [("https://example.com/known", png(b"site"), 16)]
        )
        browser = FakeBrowser(icons)
        resolved = self.store([browser]).resolve(
            [FakeItem("https://example.com/never-visited", browser)]
        )

        self.assertIn("https://example.com/never-visited", resolved)

    def test_the_nearest_size_to_the_preferred_one_wins(self):
        icons = make_chromium_icons(
            self.root / "Favicons",
            [
                ("https://example.com/a", png(b"tiny"), 8),
                ("https://example.com/a", png(b"good"), 32),
                ("https://example.com/a", png(b"huge"), 256),
            ],
        )
        browser = FakeBrowser(icons)
        resolved = self.store([browser]).resolve([FakeItem("https://example.com/a", browser)])

        self.assertEqual(Path(resolved["https://example.com/a"]).read_bytes(), png(b"good"))

    def test_an_unknown_site_resolves_to_nothing(self):
        icons = make_chromium_icons(
            self.root / "Favicons", [("https://example.com/a", png(b"one"), 16)]
        )
        browser = FakeBrowser(icons)
        self.assertEqual(
            self.store([browser]).resolve([FakeItem("https://elsewhere.test/x", browser)]), {}
        )


class FirefoxIcons(TempCache):
    def test_icons_come_from_the_firefox_store(self):
        icons = make_firefox_icons(
            self.root / "favicons.sqlite", [("https://example.com/a", png(b"ff"), 32)]
        )
        browser = FakeBrowser(icons, chromium=False)
        resolved = self.store([browser]).resolve([FakeItem("https://example.com/a", browser)])

        self.assertEqual(Path(resolved["https://example.com/a"]).read_bytes(), png(b"ff"))

    def test_an_svg_icon_is_skipped(self):
        """Firefox stores SVG icons, which Flow Launcher cannot draw."""
        icons = make_firefox_icons(
            self.root / "favicons.sqlite", [("https://example.com/a", SVG, 32)]
        )
        browser = FakeBrowser(icons, chromium=False)
        self.assertEqual(
            self.store([browser]).resolve([FakeItem("https://example.com/a", browser)]), {}
        )


class Caching(TempCache):
    def setUp(self):
        super().setUp()
        self.icons = make_chromium_icons(
            self.root / "Favicons", [("https://example.com/a", png(b"one"), 16)]
        )
        self.browser = FakeBrowser(self.icons)
        self.item = FakeItem("https://example.com/a", self.browser)

    def test_the_database_is_not_opened_again_for_a_known_url(self):
        store = self.store([self.browser])
        store.resolve([self.item])
        self.assertEqual(self.browser.copies, 1)

        store.resolve([self.item])
        self.assertEqual(self.browser.copies, 1, "a warm lookup must not touch the icon DB")

    def test_the_cache_survives_a_new_process(self):
        first = self.store([self.browser])
        expected = first.resolve([self.item])[self.item.url]

        second = self.store([self.browser])  # a fresh keystroke, in effect
        self.assertEqual(second.resolve([self.item])[self.item.url], expected)
        self.assertEqual(self.browser.copies, 1)

    def test_identical_images_are_written_once(self):
        icons = make_chromium_icons(
            self.root / "Favicons2",
            [
                ("https://example.com/a", png(b"same"), 16),
                ("https://other.test/b", png(b"same"), 16),
            ],
        )
        browser = FakeBrowser(icons)
        resolved = self.store([browser]).resolve(
            [FakeItem("https://example.com/a", browser), FakeItem("https://other.test/b", browser)]
        )

        self.assertEqual(resolved["https://example.com/a"], resolved["https://other.test/b"])
        files = [p for p in (self.cache / "favicons").iterdir() if p.suffix == ".png"]
        self.assertEqual(len(files), 1)

    def test_a_deleted_icon_file_is_fetched_again(self):
        store = self.store([self.browser])
        path = Path(store.resolve([self.item])[self.item.url])
        path.unlink()

        again = self.store([self.browser]).resolve([self.item])
        self.assertTrue(Path(again[self.item.url]).exists())
        self.assertEqual(self.browser.copies, 2)

    def test_a_damaged_index_is_survivable(self):
        self.store([self.browser]).resolve([self.item])
        (self.cache / "favicons" / "index.json").write_text("{not json", encoding="utf-8")

        resolved = self.store([self.browser]).resolve([self.item])
        self.assertIn(self.item.url, resolved)

    def test_the_index_stays_bounded(self):
        index_path = self.cache / "favicons" / "index.json"
        self.store([self.browser]).resolve([self.item])

        store = self.store([self.browser])
        index = store._load_index()
        for number in range(MAX_INDEX_ENTRIES + 10):
            index[f"https://filler.test/{number}"] = "filler.png"
        store._dirty = True
        store._save_index(index)

        saved = self.store([self.browser])._load_index()
        self.assertLessEqual(len(saved), MAX_INDEX_ENTRIES)


class QueryShape(TempCache):
    """The lookups must stay cheap: this is what keeps favicons off the keystroke."""

    class Recorder:
        def __init__(self, connection):
            self._connection = connection
            self.statements = []

        def execute(self, sql, parameters=()):
            self.statements.append((" ".join(sql.split()), tuple(parameters)))
            return self._connection.execute(sql, parameters)

        def close(self):
            self._connection.close()

    def record(self, icons, urls):
        from favicons import _chromium_lookup, _open, _pick_best

        self.statements = []
        connection = _open(str(icons))
        recorder = self.Recorder(connection)
        try:
            rows = _chromium_lookup(recorder, [], urls)
        finally:
            recorder.close()
        return recorder.statements, _pick_best(rows)

    def test_no_like_lookups(self):
        """LIKE cannot use the page_url index, so it becomes a full scan."""
        icons = make_chromium_icons(
            self.root / "Favicons", [("https://example.com/a", png(b"one"), 16)]
        )
        statements, _rows = self.record(icons, ["https://example.com/other"])
        self.assertFalse([s for s, _ in statements if "LIKE" in s.upper()])

    def test_one_seek_per_site_rather_than_one_scan(self):
        icons = make_chromium_icons(
            self.root / "Favicons", [("https://example.com/a", png(b"one"), 16)]
        )
        urls = [f"https://site{number}.test/page" for number in range(10)]
        statements, _rows = self.record(icons, urls)

        # https and http per site, and nothing more.
        self.assertEqual(len(statements), 20)
        self.assertTrue(all(">= ?" in sql for sql, _ in statements))
        self.assertFalse([sql for sql, _ in statements if sql.count(" OR ") > 1])


class Failures(TempCache):
    def test_a_browser_without_an_icon_store_is_silent(self):
        class NoIcons:
            timestamp_type = "chromium"

            def copy_icons_database(self):
                return None

        self.assertEqual(
            self.store([NoIcons()]).resolve([FakeItem("https://example.com/a", NoIcons())]), {}
        )

    def test_a_corrupt_database_yields_no_icons_and_no_raise(self):
        broken = self.root / "Favicons"
        broken.write_bytes(b"this is not a database")
        browser = FakeBrowser(broken)

        self.assertEqual(
            self.store([browser]).resolve([FakeItem("https://example.com/a", browser)]), {}
        )

    def test_a_database_missing_the_tables_yields_no_icons(self):
        empty = self.root / "Favicons"
        sqlite3.connect(empty).execute("CREATE TABLE unrelated(x INTEGER)").connection.close()
        browser = FakeBrowser(empty)

        self.assertEqual(
            self.store([browser]).resolve([FakeItem("https://example.com/a", browser)]), {}
        )

    def test_an_item_without_a_browser_is_skipped(self):
        self.assertEqual(self.store().resolve([FakeItem("https://example.com/a")]), {})

    def test_nothing_to_resolve(self):
        self.assertEqual(self.store().resolve([]), {})


class AlternateHost(TempCache):
    """A site that redirects between `example.com` and `www.example.com`."""

    def resolved(self, history_url, filed_url):
        icons = make_chromium_icons(self.root / "Favicons", [(filed_url, png(b"site"), 32)])
        browser = FakeBrowser(icons)
        return self.store([browser]).resolve([FakeItem(history_url, browser)])

    def test_a_www_mismatch_still_finds_the_icon(self):
        self.assertEqual(
            list(self.resolved("https://www.wide.video/app/1/", "https://wide.video/")),
            ["https://www.wide.video/app/1/"],
        )

    def test_the_reverse_mismatch_also_works(self):
        self.assertEqual(
            list(self.resolved("https://autocompressor.net/", "https://www.autocompressor.net/")),
            ["https://autocompressor.net/"],
        )

    def test_a_different_site_is_never_borrowed_from(self):
        self.assertEqual(self.resolved("https://a.example/x", "https://b.example/"), {})

    def test_the_port_is_kept_when_the_host_is_rewritten(self):
        import favicons

        self.assertEqual(
            favicons._rewrite_host("https://example.com:8080/a?b=1", "www.example.com"),
            "https://www.example.com:8080/a?b=1",
        )


class BrowserIconPaths(unittest.TestCase):
    """browsers.Browser finds the icon store next to the history database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def browser_for(self, engine):
        import browsers

        instance = object.__new__(browsers.Browser)
        instance.name = "test"
        instance.timestamp_type = "chromium" if engine == "chromium" else "unix_us"
        instance.database_path = self.profile / ("History" if engine == "chromium" else "places.sqlite")
        return instance

    def test_chromium_uses_the_favicons_file(self):
        (self.profile / "Favicons").write_bytes(b"db")
        self.assertEqual(
            self.browser_for("chromium").icons_database(), self.profile / "Favicons"
        )

    def test_firefox_uses_favicons_sqlite(self):
        (self.profile / "favicons.sqlite").write_bytes(b"db")
        self.assertEqual(
            self.browser_for("firefox").icons_database(), self.profile / "favicons.sqlite"
        )

    def test_no_store_means_none(self):
        self.assertIsNone(self.browser_for("chromium").icons_database())


if __name__ == "__main__":
    unittest.main()
