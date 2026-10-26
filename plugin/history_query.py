# plugin/history_query.py
"""The SQLite reads behind one browser profile's history search.

Why this is not a single statement
----------------------------------
Ordering by "last visit time" is only cheap when SQLite can walk a time index.
Walking that index is fast -- and exact -- while a search matches rows quickly.
But it is the worst possible plan for a search that matches *nothing*: the
planner then visits every entry of the index and performs one random row lookup
per entry, which on a large history takes about a second.

A plain table scan behaves the opposite way: it is never the fastest plan, but
its cost depends only on the size of the table, never on what the search term
matches. And "nothing matches yet" is exactly the state of a search box while
the user is still typing the first letters of a rare word.

So a search is a *ladder* of attempts over progressively older history, ending
with a full-history scan:

    30 days -> 90 days -> 365 days -> all history

Every rung is a superset of the one before it, and the last rung is exact. The
first rung that fills the row budget therefore already holds the newest rows,
which is all the caller displays. When a rung finds nothing at all, the term is
rare in recent history, so climbing further only adds index walking -- the
ladder jumps straight to the exact full-history scan instead.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

CHROMIUM_EPOCH_OFFSET_SECONDS = 11_644_473_600
MICROSECONDS_PER_SECOND = 1_000_000
SECONDS_PER_DAY = 86_400

#: Recency windows tried before falling back to a full-history scan, in days.
RECENCY_WINDOWS_IN_DAYS: Tuple[int, ...] = (30, 90, 365)

#: A window that yields less than this share of the row budget means the search
#: is sparse in recent history. Climbing to a wider window then costs a nearly
#: complete index walk for nothing, while a single sequential scan of the whole
#: table is exact and bounded -- so a sparse window jumps straight to the scan.
SPARSE_WINDOW_YIELD = 0.25

#: How many rows to ask a recency walk for, as a multiple of the row budget.
#: A visit walk returns one row per visit, so a single heavily re-visited site
#: can occupy many rows; asking for a few pages' worth keeps one site from
#: starving the results. Reading extra rows is cheap next to not filling.
WINDOW_OVERSCAN = 4

#: (url, title, raw timestamp) as read from a history database.
HistoryRow = Tuple[str, str, int]

#: A single statement, ready for ``Cursor.execute``.
Statement = Tuple[str, Tuple[object, ...]]


class HistoryEngine:
    """The SQLite dialect of one browser family's history database."""

    #: How this database encodes timestamps; see ``Browser.normalize_timestamp``.
    timestamp_type = ""

    #: Columns holding the URL and the page title, qualified as the JOIN in the
    #: window statement needs them.
    url_column = ""
    title_column = ""

    #: The table and column whose index makes a *bounded* recency walk possible.
    #: Empty when this engine has no index that a walk could usefully use.
    time_index_table = ""
    time_index_column = ""

    def window_start(self, window_days: int, now: float) -> int:
        """Raw timestamp of the oldest row a `window_days` window may contain."""
        oldest_unix = now - window_days * SECONDS_PER_DAY
        if self.timestamp_type == "chromium":
            oldest_unix += CHROMIUM_EPOCH_OFFSET_SECONDS
        return int(oldest_unix * MICROSECONDS_PER_SECOND)

    def has_time_index(self, connection) -> bool:
        """Whether an index can serve a time-ordered walk of this database.

        A profile written by an unusual or very old browser build may lack the
        index. Walking is then impossible, so the ladder is skipped entirely:
        the full-history scan is both the only option and a good one.
        """
        if not self.time_index_table:
            return False
        indexes = connection.execute(f"PRAGMA index_list('{self.time_index_table}')")
        for index in indexes.fetchall():
            index_name = index[1]
            columns = connection.execute(f"PRAGMA index_info('{index_name}')").fetchall()
            for sequence, _table_column_id, column_name in columns:
                if sequence == 0 and column_name == self.time_index_column:
                    return True
        return False

    def window_row_limit(self, row_budget: int) -> int:
        """How many rows the recency walk should request for this budget."""
        return row_budget

    def window_statement(
        self,
        window_days: int,
        terms: Sequence[str],
        blocked_domains: Sequence[str],
        row_limit: int,
        now: float,
    ) -> Statement:
        raise NotImplementedError

    def full_statement(
        self,
        terms: Sequence[str],
        blocked_domains: Sequence[str],
        limit: int,
    ) -> Statement:
        raise NotImplementedError

    def _filters(
        self, terms: Sequence[str], blocked_domains: Sequence[str]
    ) -> Tuple[List[str], List[object]]:
        """The search conditions shared by every statement of this engine.

        Every word must appear in the title or the URL, and no blocked domain
        may appear in the URL. Values are always bound as parameters.
        """
        conditions: List[str] = []
        parameters: List[object] = []

        for term in terms:
            conditions.append(f"({self.title_column} LIKE ? OR {self.url_column} LIKE ?)")
            parameters.extend([f"%{term}%", f"%{term}%"])

        for domain in blocked_domains:
            conditions.append(f"{self.url_column} NOT LIKE ?")
            parameters.append(f"%{domain}%")

        return conditions, parameters


class ChromiumHistoryEngine(HistoryEngine):
    """Chrome, Edge, Brave, Vivaldi, Opera, Arc, and friends.

    ``urls`` holds one row per URL with its latest visit, which is exactly one
    row per result. ``visits`` holds one row per visit and is the only table with
    a time index, so reading the newest history in order means walking
    ``visits`` and joining ``urls`` for every entry.

    That join is what rules out a recency walk here. Measured on a 100k-URL
    profile, proving that a term matches nothing costs 66 ms through the
    ``visits`` index and 13 ms by scanning ``urls``; a term matching well costs
    15 ms against 20 ms. The walk only wins when the very first index entries
    match, and loses by up to 5x when they do not. Scanning ``urls`` once costs
    the same whatever the term matches, so that is the only plan used.
    """

    timestamp_type = "chromium"
    url_column = "urls.url"
    title_column = "urls.title"
    time_index_table = ""
    time_index_column = ""

    def full_statement(self, terms, blocked_domains, limit) -> Statement:
        conditions, parameters = self._filters(terms, blocked_domains)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(limit)
        sql = (
            "SELECT url, title, last_visit_time "
            f"FROM urls{where} "
            "ORDER BY last_visit_time DESC LIMIT ?"
        )
        return sql, tuple(parameters)


class FirefoxHistoryEngine(HistoryEngine):
    """Firefox, Zen, Floorp, and Waterfox.

    ``moz_places`` already holds one row per URL together with its latest visit,
    so joining ``moz_historyvisits`` only multiplies rows (one per visit) for a
    caller that de-duplicates by URL anyway. And because the URL, the title and
    the time index all live in that one table, walking the index costs a row
    lookup rather than a join -- which is what makes the recency ladder pay off
    here: filling a page of recent matches takes 2-5 ms against 33 ms for the
    scan, and the search that used to walk every visit in the whole table
    (about a second) now ends in one scan.
    """

    timestamp_type = "unix_us"
    url_column = "url"
    title_column = "title"
    time_index_table = "moz_places"
    time_index_column = "last_visit_date"

    def window_statement(self, window_days, terms, blocked_domains, row_limit, now) -> Statement:
        conditions, parameters = self._filters(terms, blocked_domains)
        conditions.insert(0, "last_visit_date >= ?")
        parameters.insert(0, self.window_start(window_days, now))
        parameters.append(row_limit)
        sql = (
            "SELECT url, title, last_visit_date FROM moz_places "
            f"WHERE last_visit_date IS NOT NULL AND {' AND '.join(conditions)} "
            "ORDER BY last_visit_date DESC LIMIT ?"
        )
        return sql, tuple(parameters)

    def full_statement(self, terms, blocked_domains, limit) -> Statement:
        conditions, parameters = self._filters(terms, blocked_domains)
        conditions.insert(0, "last_visit_date IS NOT NULL")
        parameters.append(limit)
        # The leading '+' disables the index for ordering: it forces a single
        # sequential scan with one sort, instead of a random row lookup per
        # index entry. Same answer, bounded cost.
        sql = (
            "SELECT url, title, last_visit_date FROM moz_places "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY +last_visit_date DESC LIMIT ?"
        )
        return sql, tuple(parameters)


CHROMIUM_ENGINE = ChromiumHistoryEngine()
FIREFOX_ENGINE = FirefoxHistoryEngine()


def engine_for(timestamp_type: str) -> HistoryEngine:
    """Look up the engine that reads a database with the given timestamp encoding."""
    if timestamp_type == CHROMIUM_ENGINE.timestamp_type:
        return CHROMIUM_ENGINE
    if timestamp_type == FIREFOX_ENGINE.timestamp_type:
        return FIREFOX_ENGINE
    raise ValueError(f"Unsupported history timestamp type: {timestamp_type}")


def build_statements(
    engine: HistoryEngine,
    connection,
    terms: Sequence[str],
    blocked_domains: Sequence[str],
    limit: int,
    windows: Sequence[int] = RECENCY_WINDOWS_IN_DAYS,
    now: Optional[float] = None,
) -> List[Statement]:
    """The ladder of statements to try, cheapest first; the last one is exact."""
    now = time.time() if now is None else now
    statements: List[Statement] = []

    if engine.has_time_index(connection):
        for window_days in windows:
            statements.append(
                engine.window_statement(
                    window_days,
                    terms,
                    blocked_domains,
                    engine.window_row_limit(limit),
                    now,
                )
            )

    statements.append(engine.full_statement(terms, blocked_domains, limit))
    return statements


def newest_per_url(rows: Sequence[HistoryRow]) -> List[HistoryRow]:
    """One row per URL -- the one with the latest visit -- newest first."""
    newest: dict = {}
    for row in rows:
        url, _title, visited_at = row
        known = newest.get(url)
        if known is None or visited_at > known[2]:
            newest[url] = row
    return sorted(newest.values(), key=lambda row: row[2], reverse=True)


def read_history(
    connection,
    engine: HistoryEngine,
    terms: Sequence[str],
    blocked_domains: Sequence[str],
    limit: int,
    windows: Sequence[int] = RECENCY_WINDOWS_IN_DAYS,
    now: Optional[float] = None,
) -> List[HistoryRow]:
    """Read the newest distinct URLs matching `terms`, trying recent history first.

    Returns at most `limit` rows, one per URL, ordered by descending visit time.
    These are the newest matches in the whole history: the recency windows only
    ever shorten the search, never change its answer.
    """
    if limit <= 0:
        return []

    statements = build_statements(
        engine, connection, terms, blocked_domains, limit, windows, now
    )

    for position, (sql, parameters) in enumerate(statements):
        is_full_history = position == len(statements) - 1
        rows = newest_per_url(connection.execute(sql, parameters).fetchall())

        if is_full_history or len(rows) >= limit:
            return rows[:limit]
        if len(rows) < limit * SPARSE_WINDOW_YIELD:
            # Too little in this window to be worth widening: one sequential
            # scan is exact and bounded, the wider windows are neither.
            break

    sql, parameters = statements[-1]
    return newest_per_url(connection.execute(sql, parameters).fetchall())[:limit]
