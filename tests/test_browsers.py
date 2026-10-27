# tests/test_browsers.py
"""Copying a browser database: the log is part of the state."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from browsers import Browser  # noqa: E402


class WriteAheadLog(unittest.TestCase):
    """SQLite can hold recent writes outside the main file.

    A copy that takes only the main file reads the state as of the last
    checkpoint -- and because that file's timestamp does not move either, the
    copy would look fresh forever.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.cache = self.root / "Cache"
        self.cache.mkdir()

        self.database = self.profile / "places.sqlite"
        self.writer = sqlite3.connect(self.database)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT)")
        self.writer.commit()
        self.writer.execute("INSERT INTO moz_places(url) VALUES ('https://first.example/')")
        self.writer.commit()
        self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def tearDown(self):
        self.writer.close()
        self._tmp.cleanup()

    def browser(self):
        return Browser(
            name="firefox",
            cache_dir=self.cache,
            timestamp_type="unix_us",
            custom_path=self.profile,
            db_file="places.sqlite",
        )

    def visit(self, url):
        """A write the browser has not checkpointed yet."""
        self.writer.execute("INSERT INTO moz_places(url) VALUES (?)", (url,))
        self.writer.commit()

    def urls(self, copy: str):
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            return [row[0] for row in connection.execute("SELECT url FROM moz_places")]
        finally:
            connection.close()

    def test_the_log_is_still_there(self):
        """Guard: without a log on disk these tests would prove nothing."""
        self.visit("https://second.example/")
        self.assertTrue(Path(str(self.database) + "-wal").exists())

    def test_a_page_visited_after_the_last_checkpoint_is_in_the_copy(self):
        self.visit("https://second.example/")
        self.assertIn("https://second.example/", self.urls(self.browser()._copy_database()))

    def test_a_new_write_is_seen_even_though_the_main_file_did_not_change(self):
        browser = self.browser()
        browser._copy_database()  # warm the cache
        unchanged = self.database.stat().st_mtime

        self.visit("https://second.example/")
        self.assertEqual(self.database.stat().st_mtime, unchanged)

        self.assertIn("https://second.example/", self.urls(browser._copy_database()))

    def test_a_copy_is_not_fresh_once_the_browser_has_dropped_its_log(self):
        """The copy has a log and the browser does not: they disagree."""
        browser = self.browser()
        first = Path(browser._copy_database())
        self.assertTrue(Path(str(first) + "-wal").exists())

        # The browser quits: the log is folded into the main file and removed.
        self.writer.close()
        self.assertFalse(Path(str(self.database) + "-wal").exists())

        self.assertFalse(browser._copy_is_fresh(self.database, first))
        self.assertIn("https://first.example/", self.urls(str(browser._copy_database())))

    def test_a_copy_is_not_fresh_when_the_log_grew_in_the_same_tick(self):
        """Two writes can share one timestamp; sizes still tell them apart."""
        browser = self.browser()
        first = Path(browser._copy_database())
        first_log_size = Path(str(first) + "-wal").stat().st_size

        self.visit("https://second.example/")

        self.assertNotEqual(Path(str(self.database) + "-wal").stat().st_size, first_log_size)
        self.assertFalse(browser._copy_is_fresh(self.database, first))

    def test_repeated_copies_stay_readable(self):
        browser = self.browser()
        for index in range(3):
            self.visit(f"https://visit-{index}.example/")
            self.assertIn(
                f"https://visit-{index}.example/", self.urls(browser._copy_database())
            )


class TornCopy(unittest.TestCase):
    """The browser is writing while the copy is made."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.cache = self.root / "Cache"
        self.cache.mkdir()
        self.database = self.profile / "History"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT)")
        connection.execute("INSERT INTO urls(url) VALUES ('https://example.com/a')")
        connection.commit()
        connection.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_torn_copy_is_taken_again(self):
        import browsers

        original = browsers.shutil.copy
        calls = {"n": 0}

        def copy(source, target):
            calls["n"] += 1
            if calls["n"] == 1:
                # A database caught mid-transaction: a header, then rubbish.
                Path(target).write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
                return target
            return original(source, target)

        browsers.shutil.copy = copy
        try:
            browser = browsers.Browser(
                name="chrome",
                cache_dir=self.cache,
                timestamp_type="chromium",
                custom_path=self.profile,
                db_file="History",
            )
            target = browser._copy_database()
        finally:
            browsers.shutil.copy = original

        self.assertGreaterEqual(calls["n"], 2)
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT url FROM urls")],
                ["https://example.com/a"],
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
