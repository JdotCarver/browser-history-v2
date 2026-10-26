# tools/perf/diagnose.py
"""Measure this machine's real browser history without reporting its contents.

Synthetic fixtures can be wrong in ways that matter: real profiles have
different sizes, different visit densities and different indexes. This script
measures the real thing.

What it prints: profile names as the plugin knows them ("brave_profile_0"),
database sizes, row counts, index names, and timings.
What it never prints: URLs, page titles, search terms, or home directories. Its
output is published to the `actions-logs` branch, which is not private, so the
terms used for the timings are labelled by *what they are* ("a term matching
nothing") and never echoed.

Usage
-----
    python tools/perf/diagnose.py
    python tools/perf/diagnose.py --json
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import machine_label  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugin"))

import browsers  # noqa: E402
import results  # noqa: E402
from history_query import CHROMIUM_ENGINE, FIREFOX_ENGINE, read_history  # noqa: E402

#: Long enough that nothing real matches it, short enough to stay meaningful.
ABSENT_TERM = "qzxqzxqzx"

#: Words too common in titles to say anything about match density.
STOP_WORDS = frozenset(
    "the and for you with are not from this that all can has its our your "
    "http https www com html htm php index page site".split()
)


def discover_profiles(cache_dir: Path):
    """Every profile the plugin would find on this machine."""
    found = []
    names = (
        list(browsers.CHROMIUM_PROFILE_BASES)
        + list(browsers.FIREFOX_BASES)
        + list(browsers.FIXED_PATHS)
    )
    for name in names:
        try:
            found.extend(browsers.get_all_profiles(name, cache_dir))
        except (ValueError, OSError):
            continue
    return found


def frequent_term(connection, engine, sample_rows: int = 2000) -> tuple[str, int]:
    """A word that appears in many recent titles, for a "matches a lot" timing.

    The word is returned to the caller but never printed: the caller uses it for
    a query and reports only the match count.
    """
    column = "title"
    table = "urls" if engine is CHROMIUM_ENGINE else "moz_places"
    time_column = "last_visit_time" if engine is CHROMIUM_ENGINE else "last_visit_date"
    try:
        rows = connection.execute(
            f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL "
            f"ORDER BY {time_column} DESC LIMIT ?",
            (sample_rows,),
        ).fetchall()
    except sqlite3.Error:
        return "", 0

    counts = Counter()
    for (title,) in rows:
        for word in str(title).lower().split():
            word = "".join(character for character in word if character.isalnum())
            if len(word) >= 4 and word not in STOP_WORDS:
                counts[word] += 1
    if not counts:
        return "", 0
    return counts.most_common(1)[0][0], counts.most_common(1)[0][1]


def pick_search_term(cache_dir: Path) -> str:
    """The most frequent word in the largest real profile's recent titles.

    Returns "" when there is no history to draw from. The caller times a search
    with this word and reports the timing only: the word itself is history
    content and never leaves the machine.
    """
    profiles = discover_profiles(cache_dir)
    if not profiles:
        return ""
    largest = max(profiles, key=lambda browser: Path(browser.database_path).stat().st_size)
    connection = sqlite3.connect(f"file:{largest._copy_database()}?mode=ro", uri=True)
    try:
        term, _occurrences = frequent_term(connection, largest.engine)
    finally:
        connection.close()
    return term


def time_call(function, repeats: int = 5):
    """Median wall time of `function`, in milliseconds, plus its result."""
    samples = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 2), result


def describe_profile(browser, cache_dir: Path) -> dict:
    """Sizes, counts and indexes for one profile, using its own cached copy."""
    database = Path(browser.database_path)
    engine = browser.engine
    details = {
        "profile": browser.name,
        "engine": "chromium" if engine is CHROMIUM_ENGINE else "firefox",
        "database_mb": round(database.stat().st_size / 1e6, 1),
    }

    warm_copy_ms, copied = time_call(browser._copy_database, repeats=3)
    details["copy_cached_ms"] = warm_copy_ms

    connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
    try:
        table = "urls" if engine is CHROMIUM_ENGINE else "moz_places"
        details["rows"] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # Both interesting tables, because whether a recency walk is even
        # possible depends on where the browser put its time index.
        details["indexes"] = {
            name: sorted(
                row[1] for row in connection.execute(f"PRAGMA index_list('{name}')")
            )
            for name in ("urls", "visits", "moz_places", "moz_historyvisits")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        }
    finally:
        connection.close()

    details["recency_walk"] = bool(engine.time_index_table)
    return details


def measure_queries(browser, cache_dir: Path, limit: int = 500) -> dict:
    """Per-query timings for one profile: empty, frequent, and nothing-matching."""
    database = Path(browser._copy_database())
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    engine = browser.engine
    timings = {}
    try:
        term, occurrences = frequent_term(connection, engine)
        scenarios = [
            ("empty", "", "no search term: the idle listing of recent history"),
            ("frequent", term, "a word seen often in recent titles"),
            ("absent", ABSENT_TERM, "still typing: matches nothing"),
        ]
        for key, value, _description in scenarios:
            words = value.split()
            if key == "frequent" and not words:
                continue
            elapsed, rows = time_call(
                lambda value=value: read_history(
                    connection, engine, words, [], limit
                ),
                repeats=5,
            )
            timings[key] = {
                "ms": elapsed,
                "rows": len(rows),
                "note": f"term matches {occurrences} of the sampled titles"
                if key == "frequent"
                else "",
            }
    finally:
        connection.close()
    return timings


def measure_cold_copy(browser) -> float:
    """Time one real copy of the database into an empty cache directory."""
    with tempfile.TemporaryDirectory() as directory:
        cold_cache = Path(directory)
        original = browser.cache_dir
        browser.cache_dir = cold_cache
        try:
            elapsed, _path = time_call(browser._copy_database, repeats=1)
        finally:
            browser.cache_dir = original
    return elapsed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one JSON document")
    args = parser.parse_args(argv)

    cache_dir = Path(tempfile.mkdtemp(prefix="bh-diagnose-"))
    report = {
        "machine": {
            "platform": machine_label.describe_machine(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "cores": __import__("os").cpu_count(),
        },
        "profiles": [],
        "aggregate": {},
    }

    try:
        found = discover_profiles(cache_dir)
        if not args.json:
            print(f"machine: {report['machine']}")
            print(f"profiles found: {len(found)}")
            print()

        for browser in found:
            try:
                details = describe_profile(browser, cache_dir)
                details["queries"] = measure_queries(browser, cache_dir)
                details["copy_cold_ms"] = measure_cold_copy(browser)
            except (OSError, sqlite3.Error) as error:
                report["profiles"].append(
                    {"profile": browser.name, "error": f"{type(error).__name__}: {error}"}
                )
                continue
            report["profiles"].append(details)

            if not args.json:
                print(f"== {details['profile']} ({details['engine']})")
                print(f"   database: {details['database_mb']} MB, {details['rows']:,} rows")
                print(f"   recency walk used: {details['recency_walk']}")
                for table, names in details["indexes"].items():
                    print(f"   indexes on {table}: {', '.join(names) or '(none)'}")
                print(f"   copy: {details['copy_cold_ms']} ms cold,"
                      f" {details['copy_cached_ms']} ms cached")
                for key, query in details["queries"].items():
                    note = f"  ({query['note']})" if query["note"] else ""
                    print(f"   query {key:<9} {query['ms']:>8.2f} ms"
                          f"  rows={query['rows']}{note}")
                print()

        if found:
            healthy = [p for p in report["profiles"] if "queries" in p]
            if healthy:
                report["aggregate"] = {
                    "profiles": len(healthy),
                    "total_empty_ms": round(sum(p["queries"]["empty"]["ms"] for p in healthy), 2),
                    "total_absent_ms": round(
                        sum(p["queries"].get("absent", {}).get("ms", 0) for p in healthy), 2
                    ),
                    "largest_database_mb": max(p["database_mb"] for p in healthy),
                }
                if not args.json:
                    print(f"aggregate: {report['aggregate']}")
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
