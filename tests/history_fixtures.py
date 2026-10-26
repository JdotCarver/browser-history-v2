# tests/history_fixtures.py
"""Throwaway history databases that copy the real Chromium and Firefox schemas.

The point of these fixtures is to be *independent* of the code under test: they
build a database and can answer "what should the search have returned?" with a
deliberately naive full-history query. Tests then assert that the optimised
reader produces the same answer as the naive one.

Only the tables and indexes that the plugin actually touches are modelled.
"""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

CHROMIUM_EPOCH_OFFSET_SECONDS = 11_644_473_600
MICROSECONDS_PER_SECOND = 1_000_000
SECONDS_PER_DAY = 86_400

WORDS = (
    "python sqlite flow launcher browser history performance github stack "
    "overflow youtube music reddit news rust kernel docs api design test build "
    "measure profile cache plugin"
).split()

HOSTS = (
    "github.com",
    "youtube.com",
    "stackoverflow.com",
    "reddit.com",
    "news.ycombinator.com",
    "docs.python.org",
    "developer.mozilla.org",
    "wikipedia.org",
)

CHROMIUM_SCHEMA = (
    """CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR)""",
    """CREATE TABLE urls(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url LONGVARCHAR,
        title LONGVARCHAR,
        visit_count INTEGER DEFAULT 0 NOT NULL,
        typed_count INTEGER DEFAULT 0 NOT NULL,
        last_visit_time INTEGER NOT NULL,
        hidden INTEGER DEFAULT 0 NOT NULL)""",
    """CREATE TABLE visits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url INTEGER NOT NULL,
        visit_time INTEGER NOT NULL,
        from_visit INTEGER,
        transition INTEGER DEFAULT 0 NOT NULL,
        segment_id INTEGER,
        visit_duration INTEGER DEFAULT 0 NOT NULL,
        incremented_omnibox_typed_score INTEGER DEFAULT 0 NOT NULL,
        opener_visit INTEGER)""",
    "CREATE UNIQUE INDEX urls_url_index ON urls (url)",
    "CREATE INDEX visits_url_index ON visits (url)",
)

FIREFOX_SCHEMA = (
    """CREATE TABLE moz_places(
        id INTEGER PRIMARY KEY,
        url LONGVARCHAR,
        title LONGVARCHAR,
        rev_host LONGVARCHAR,
        visit_count INTEGER DEFAULT 0,
        hidden INTEGER DEFAULT 0 NOT NULL,
        typed INTEGER NOT NULL DEFAULT 0,
        frecency INTEGER DEFAULT -1 NOT NULL,
        last_visit_date INTEGER,
        guid TEXT,
        foreign_count INTEGER DEFAULT 0 NOT NULL,
        url_hash INTEGER DEFAULT 0 NOT NULL,
        description TEXT,
        preview_image_url TEXT,
        site_name TEXT,
        origin_id INTEGER,
        recalc_frecency INTEGER NOT NULL DEFAULT 0,
        alt_frecency INTEGER)""",
    """CREATE TABLE moz_historyvisits(
        id INTEGER PRIMARY KEY,
        from_visit INTEGER,
        place_id INTEGER,
        visit_date INTEGER,
        visit_type INTEGER,
        session INTEGER DEFAULT 0,
        source INTEGER NOT NULL DEFAULT 0,
        triggeringPlaceId INTEGER)""",
    "CREATE UNIQUE INDEX moz_places_url_uniqueindex ON moz_places(url)",
    "CREATE INDEX moz_historyvisits_placedateindex ON moz_historyvisits(place_id, visit_date)",
)


def _visits_for(rng: random.Random, max_age_days: float, age_skew: float) -> List[float]:
    """Ages, in days before now, for one URL's visits; the first one is the newest.

    `age_skew` of 1.0 spreads history evenly over the whole range. Real history
    is not spread evenly: people revisit recently used sites far more often, so
    a higher skew models a real profile, where the last month holds a sizeable
    share of everything ever visited.
    """
    newest_age = max_age_days * (rng.random() ** age_skew)
    ages = [newest_age]
    for _ in range(rng.randint(0, 3)):
        ages.append(min(max_age_days, newest_age + rng.uniform(0, 120)))
    return ages


def _url_and_title(rng: random.Random, index: int) -> Tuple[str, str]:
    host = rng.choice(HOSTS)
    slug = "-".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3)))
    title = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 6)))
    return f"https://{host}/path/{slug}/{index}", f"{title} #{index}"


def build_chromium_history(
    path: Path,
    url_count: int = 400,
    *,
    with_time_index: bool = True,
    seed: int = 7,
    now: Optional[float] = None,
    max_age_days: float = 5 * 365,
    age_skew: float = 1.0,
) -> Path:
    """Create a Chromium `History` database with `url_count` distinct URLs."""
    if path.exists():
        path.unlink()
    now = time.time() if now is None else now
    rng = random.Random(seed)

    connection = sqlite3.connect(path)
    for statement in CHROMIUM_SCHEMA:
        connection.execute(statement)
    if with_time_index:
        connection.execute("CREATE INDEX visits_time_index ON visits (visit_time)")

    for index in range(1, url_count + 1):
        url, title = _url_and_title(rng, index)
        ages = _visits_for(rng, max_age_days, age_skew)
        newest_age = min(ages)
        last_visit = int(
            (now - newest_age * SECONDS_PER_DAY + CHROMIUM_EPOCH_OFFSET_SECONDS)
            * MICROSECONDS_PER_SECOND
        )
        connection.execute(
            "INSERT INTO urls (id, url, title, visit_count, typed_count, last_visit_time, hidden)"
            " VALUES (?, ?, ?, ?, 0, ?, 0)",
            (index, url, title, len(ages), last_visit),
        )
        for age in ages:
            # Chromium keeps urls.last_visit_time equal to the newest visit.
            visit_time = int(
                (now - age * SECONDS_PER_DAY + CHROMIUM_EPOCH_OFFSET_SECONDS)
                * MICROSECONDS_PER_SECOND
            ) if age != newest_age else last_visit
            connection.execute(
                "INSERT INTO visits (url, visit_time, from_visit, transition,"
                " visit_duration, incremented_omnibox_typed_score)"
                " VALUES (?, ?, 0, 0, 0, 0)",
                (index, visit_time),
            )

    connection.commit()
    connection.close()
    return path


def build_firefox_places(
    path: Path,
    url_count: int = 400,
    *,
    with_time_index: bool = True,
    seed: int = 11,
    now: Optional[float] = None,
    max_age_days: float = 5 * 365,
    age_skew: float = 1.0,
) -> Path:
    """Create a Firefox `places.sqlite` with `url_count` distinct places."""
    if path.exists():
        path.unlink()
    now = time.time() if now is None else now
    rng = random.Random(seed)

    connection = sqlite3.connect(path)
    for statement in FIREFOX_SCHEMA:
        connection.execute(statement)
    if with_time_index:
        connection.execute(
            "CREATE INDEX moz_places_lastvisitdateindex ON moz_places (last_visit_date)"
        )

    for index in range(1, url_count + 1):
        url, title = _url_and_title(rng, index)
        ages = _visits_for(rng, max_age_days, age_skew)
        newest_age = min(ages)
        last_visit = int((now - newest_age * SECONDS_PER_DAY) * MICROSECONDS_PER_SECOND)
        connection.execute(
            "INSERT INTO moz_places (id, url, title, visit_count, hidden, typed, frecency,"
            " last_visit_date, guid, foreign_count, url_hash, recalc_frecency)"
            " VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, 0, ?, 0)",
            (index, url, title, len(ages), last_visit, f"guid{index:08d}", index),
        )
        for age in ages:
            visit_date = int((now - age * SECONDS_PER_DAY) * MICROSECONDS_PER_SECOND)
            connection.execute(
                "INSERT INTO moz_historyvisits (place_id, visit_date, visit_type)"
                " VALUES (?, ?, 1)",
                (index, visit_date),
            )

    connection.commit()
    connection.close()
    return path


#: The statement each engine uses for an exhaustive search. Deliberately naive:
#: a full scan of every row, ordered in memory. This is the answer to match.
_REFERENCE_QUERIES = {
    "chromium": {
        "base": "SELECT url, title, last_visit_time FROM urls",
        "conditions": (),
    },
    "firefox": {
        "base": "SELECT url, title, last_visit_date FROM moz_places",
        "conditions": ("last_visit_date IS NOT NULL",),
    },
}


def reference_rows(
    db_path: Path,
    engine: str,
    terms: Sequence[str] = (),
    blocked_domains: Sequence[str] = (),
    limit: int = 100,
) -> List[Tuple[str, str, int]]:
    """What an exhaustive, unoptimised search returns: newest distinct URLs first."""
    query = _REFERENCE_QUERIES[engine]
    base = query["base"]
    url_column, title_column = "url", "title"
    conditions: List[str] = list(query["conditions"])
    parameters: List[object] = []
    for term in terms:
        conditions.append(f"({title_column} LIKE ? OR {url_column} LIKE ?)")
        parameters.extend([f"%{term}%", f"%{term}%"])
    for domain in blocked_domains:
        conditions.append(f"{url_column} NOT LIKE ?")
        parameters.append(f"%{domain}%")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(f"{base}{where}", tuple(parameters)).fetchall()
    finally:
        connection.close()

    newest = {}
    for row in rows:
        known = newest.get(row[0])
        if known is None or row[2] > known[2]:
            newest[row[0]] = row
    return sorted(newest.values(), key=lambda row: row[2], reverse=True)[:limit]
