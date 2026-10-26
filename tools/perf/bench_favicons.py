#!/usr/bin/env python3
"""Measure what favicons add to a keystroke, in isolation.

The feature only costs something if it slows the listing down, so this
benchmark builds a realistic icon database and times the store directly rather
than inferring a cost from the plugin's total.

Three paths matter, and the last is the one a user feels on every keystroke:

    cold    first search after the browser fetched a new icon: the icon DB is
            copied, queried, and the icons are written out
    warm    the index already knows every URL: no database is opened at all
    miss    the index knows nothing and the browser has no icon for these
            sites, so every lookup comes back empty

Usage:
    python tools/perf/bench_favicons.py [--urls 25] [--rows 20000]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin"))

from favicons import FaviconStore  # noqa: E402

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def icon_bytes(seed: int, size: int = 32) -> bytes:
    """A distinct PNG for each site, roughly the size of a real 32px icon."""
    payload = (f"icon-{seed}".encode() * 24)[:512]
    return PNG_HEADER + payload + bytes([size]) * 64


class FakeBrowser:
    """A profile whose icon database is a copy that already exists."""

    timestamp_type = "chromium"

    def __init__(self, database: Path) -> None:
        self._database = database

    def copy_icons_database(self) -> str:
        return str(self._database)


def build_icon_database(path: Path, rows: int) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE icon_mapping(id INTEGER PRIMARY KEY, page_url TEXT NOT NULL, icon_id INTEGER);
        CREATE TABLE favicons(id INTEGER PRIMARY KEY, url TEXT NOT NULL, icon_type INTEGER DEFAULT 1);
        CREATE TABLE favicon_bitmaps(
            id INTEGER PRIMARY KEY, icon_id INTEGER NOT NULL, last_updated INTEGER DEFAULT 0,
            image_data BLOB, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0
        );
        CREATE INDEX icon_mapping_page_url_idx ON icon_mapping(page_url);
        """
    )
    rows_data = []
    for index in range(rows):
        rows_data.append((index, f"https://site{index}.test/page", icon_bytes(index), 32))
    connection.executemany(
        "INSERT INTO favicons(id, url) VALUES (?, ?)",
        [(index, f"https://site{index}.test/favicon.ico") for index, *_ in rows_data],
    )
    connection.executemany(
        "INSERT INTO icon_mapping(id, page_url, icon_id) VALUES (?, ?, ?)",
        [(index, page_url, index) for index, page_url, _data, _size in rows_data],
    )
    connection.executemany(
        "INSERT INTO favicon_bitmaps(icon_id, image_data, width, height) VALUES (?, ?, ?, ?)",
        [(index, data, size, size) for index, _page, data, size in rows_data],
    )
    connection.commit()
    connection.close()
    return path


class Item:
    def __init__(self, url: str, browser: FakeBrowser) -> None:
        self.url = url
        self.browser = browser


def timed(call, repeats: int = 5) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", type=int, default=25, help="results on screen")
    parser.add_argument("--rows", type=int, default=20000, help="sites in the icon DB")
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="bh-favicons-"))
    results = {}
    try:
        database = build_icon_database(work / "Favicons", args.rows)
        browser = FakeBrowser(database)
        known = [Item(f"https://site{index}.test/page", browser) for index in range(args.urls)]
        unknown = [Item(f"https://nowhere{index}.test/x", browser) for index in range(args.urls)]

        print(f"icon database: {args.rows} sites, {database.stat().st_size / 1e6:.1f} MB")
        print(f"results on screen: {args.urls}")

        # cold: an empty cache directory, everything to do.
        def cold():
            cache = work / f"cold-{time.perf_counter_ns()}"
            FaviconStore(cache, [browser]).resolve(known)

        results["cold"] = timed(cold, repeats=5)

        # warm: the same cache, already filled.
        warm_cache = work / "warm"
        FaviconStore(warm_cache, [browser]).resolve(known)
        results["warm"] = timed(lambda: FaviconStore(warm_cache, [browser]).resolve(known))

        # miss: nothing cached and nothing to find.
        def miss():
            cache = work / f"miss-{time.perf_counter_ns()}"
            FaviconStore(cache, [browser]).resolve(unknown)

        results["miss"] = timed(miss, repeats=5)

        print()
        print(f"{'path':<8}{'median':>10}  what happens")
        print("-" * 62)
        labels = {
            "cold": "copy + query the icon DB, write icons, save the index",
            "warm": "read the index only; the icon DB is never opened",
            "miss": "the DB is queried and comes back empty",
        }
        for key in ("cold", "warm", "miss"):
            print(f"{key:<8}{results[key]:>8.2f} ms  {labels[key]}")

        print()
        print("Compare with the query work these numbers are added to:")
        print("  the listing itself is around 2-3 ms, imports are around 60-80 ms.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
