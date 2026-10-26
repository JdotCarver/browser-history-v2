# tests/test_history_query.py
"""The tiered reader must return exactly what an exhaustive search returns.

Every test here compares against `history_fixtures.reference_rows`, which runs a
deliberately naive full-history query. Speed is never asserted directly; what is
asserted is that the cheap plans and the expensive plan agree, and that the
ladder stops climbing as soon as it has a full page.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import history_fixtures  # noqa: E402
import history_query  # noqa: E402


class CountingConnection(sqlite3.Connection):
    """A connection that records every SELECT it is asked to run."""

    selects = None

    def execute(self, sql, parameters=()):
        if self.selects is not None and sql.lstrip().upper().startswith("SELECT"):
            self.selects.append(sql)
        return super().execute(sql, parameters)


def connect(path, selects=None):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, factory=CountingConnection)
    connection.selects = selects
    return connection


class HistoryQueryContract:
    """Shared behaviour, mixed into one TestCase per engine below.

    A mixin rather than a TestCase so that the contract itself is never
    collected as a test with no engine to run against.
    """

    build = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def build_history(self, name="history", **kwargs):
        return self.build(self.directory / name, **kwargs)

    def read(self, db_path, terms=(), blocked=(), limit=100):
        connection = connect(db_path)
        try:
            return history_query.read_history(
                connection,
                self.engine,
                terms=terms,
                blocked_domains=blocked,
                limit=limit,
            )
        finally:
            connection.close()

    def test_agrees_with_exhaustive_search(self):
        db_path = self.build_history(url_count=300)
        for terms in [[], ["youtube"], ["kernel"], ["zzqq"], ["python", "sqlite"], ["flow"]]:
            for limit in (5, 40, 500):
                with self.subTest(terms=terms, limit=limit):
                    self.assertEqual(
                        self.read(db_path, terms=terms, limit=limit),
                        history_fixtures.reference_rows(
                            db_path, self.engine_name, terms=terms, limit=limit
                        ),
                    )

    def test_returns_one_row_per_url_newest_first(self):
        db_path = self.build_history(url_count=200)
        rows = self.read(db_path, limit=50)
        urls = [row[0] for row in rows]
        self.assertEqual(len(urls), len(set(urls)), "the same URL appeared twice")
        self.assertEqual(rows, sorted(rows, key=lambda row: row[2], reverse=True))

    def test_limit_is_respected(self):
        db_path = self.build_history(url_count=200)
        self.assertEqual(len(self.read(db_path, limit=7)), 7)
        self.assertEqual(self.read(db_path, limit=0), [])

    def test_blocked_domains_are_excluded(self):
        db_path = self.build_history(url_count=300)
        rows = self.read(db_path, blocked=["youtube.com"], limit=500)
        self.assertTrue(rows)
        self.assertFalse([row for row in rows if "youtube.com" in row[0]])
        self.assertEqual(
            rows,
            history_fixtures.reference_rows(
                db_path, self.engine_name, blocked_domains=["youtube.com"], limit=500
            ),
        )

    def test_every_search_term_must_match(self):
        db_path = self.build_history(url_count=300)
        rows = self.read(db_path, terms=["youtube", "zzqq"], limit=50)
        self.assertEqual(rows, [])
        self.assertEqual(
            self.read(db_path, terms=["youtube", "music"], limit=50),
            history_fixtures.reference_rows(
                db_path, self.engine_name, terms=["youtube", "music"], limit=50
            ),
        )

    def test_absent_term_falls_back_to_one_full_scan(self):
        """A term matching nothing must not walk every recency window first."""
        db_path = self.build_history(url_count=300)
        selects = []
        connection = connect(db_path, selects)
        try:
            rows = history_query.read_history(
                connection,
                self.engine,
                terms=["zzqq"],
                blocked_domains=[],
                limit=100,
            )
        finally:
            connection.close()

        self.assertEqual(rows, [])
        self.assertLessEqual(
            len(selects),
            2,
            "an absent term should try the newest window once, then scan; ran:\n"
            + "\n".join(selects),
        )

    def test_recent_history_resolves_from_the_newest_window(self):
        """When recent history fills the page, one cheap statement is enough."""
        db_path = self.build_history(url_count=300, max_age_days=20)
        selects = []
        connection = connect(db_path, selects)
        try:
            rows = history_query.read_history(
                connection, self.engine, terms=[], blocked_domains=[], limit=50
            )
        finally:
            connection.close()

        self.assertEqual(len(rows), 50)
        self.assertEqual(len(selects), 1, "expected a single window query:\n" + "\n".join(selects))

    def test_works_without_a_time_index(self):
        """An old or unusual profile has no time index; the scan must still be exact."""
        db_path = self.build_history(url_count=300, with_time_index=False)
        selects = []
        connection = connect(db_path, selects)
        try:
            rows = history_query.read_history(
                connection, self.engine, terms=["youtube"], blocked_domains=[], limit=40
            )
        finally:
            connection.close()

        self.assertEqual(
            rows,
            history_fixtures.reference_rows(
                db_path, self.engine_name, terms=["youtube"], limit=40
            ),
        )
        self.assertEqual(len(selects), 1, "without an index there is nothing to walk")


class ChromiumQueryTests(HistoryQueryContract, unittest.TestCase):
    engine = history_query.CHROMIUM_ENGINE
    engine_name = "chromium"
    build = staticmethod(history_fixtures.build_chromium_history)


class FirefoxQueryTests(HistoryQueryContract, unittest.TestCase):
    engine = history_query.FIREFOX_ENGINE
    engine_name = "firefox"
    build = staticmethod(history_fixtures.build_firefox_places)


class ChromiumPlanTests(unittest.TestCase):
    """Chromium walks its time index only when there is nothing to filter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = history_fixtures.build_chromium_history(
            Path(self._tmp.name) / "History", url_count=300, with_time_index=True
        )
        self.connection = connect(self.db_path)
        self.addCleanup(self.connection.close)

    def statements_for(self, terms):
        return history_query.build_statements(
            history_query.CHROMIUM_ENGINE, self.connection, terms, [], 10
        )

    def test_the_unfiltered_listing_tries_recent_visits_first(self):
        """The newest visits are all matches, so the walk fills the page at once."""
        statements = self.statements_for([])
        self.assertGreater(len(statements), 1)
        self.assertIn("visits.visit_time >=", statements[0][0])
        self.assertIn("ORDER BY last_visit_time", statements[-1][0])

    def test_a_search_never_walks_the_visit_index(self):
        """One index entry costs a join into `urls`; on a 100k-URL profile that is
        66 ms to prove no match against 13 ms for one scan. Never walk for a search.
        Re-enabling this needs a benchmark that shows the opposite.
        """
        statements = self.statements_for(["youtube"])
        self.assertEqual(len(statements), 1)
        self.assertIn("FROM urls", statements[0][0])

    def test_an_unfiltered_listing_still_agrees_with_the_exhaustive_search(self):
        connection = connect(self.db_path)
        try:
            rows = history_query.read_history(
                connection, history_query.CHROMIUM_ENGINE, [], [], 25
            )
        finally:
            connection.close()
        self.assertEqual(
            rows, history_fixtures.reference_rows(self.db_path, "chromium", limit=25)
        )


class TimestampEncodingTests(unittest.TestCase):
    def test_chromium_window_start_is_encoded_like_the_database(self):
        engine = history_query.CHROMIUM_ENGINE
        now = 1_700_000_000.0
        expected = int(
            (now - 30 * 86_400 + history_query.CHROMIUM_EPOCH_OFFSET_SECONDS) * 1_000_000
        )
        self.assertEqual(engine.window_start(30, now), expected)

    def test_unix_microseconds_window_start(self):
        engine = history_query.FIREFOX_ENGINE
        now = 1_700_000_000.0
        self.assertEqual(engine.window_start(30, now), int((now - 30 * 86_400) * 1_000_000))


if __name__ == "__main__":
    unittest.main()
