# plugin/results.py
"""Turning per-profile reads into the single list Flow Launcher displays.

Reads run in parallel: one profile's SQLite work is independent of another's,
and SQLite releases the interpreter lock while it steps a statement, so threads
do give real concurrency here without the cost of spawning processes.

Everything in this module is deliberately free of Flow Launcher dependencies so
it can be tested on its own.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Sequence, Tuple

from browsers import Browser, HistoryItem


def _read_profile(
    browser: Browser, search_term: str, limit: int, blocked_domains: Sequence[str]
) -> Tuple[str, List[HistoryItem], Optional[str]]:
    """Read one profile, reporting failures instead of raising them.

    Never raises: a thread that dies would take the whole search with it, and
    one broken profile must not hide the results of a working one.
    """
    try:
        return (
            browser.name,
            browser.history(
                search_term=search_term,
                limit=limit,
                blocked_domains=list(blocked_domains),
            ),
            None,
        )
    except FileNotFoundError:
        return browser.name, [], None
    except OSError as error:
        return browser.name, [], f"{browser.name}: {error}"


def read_profiles(
    browsers: Sequence[Browser],
    search_term: str,
    limit: int,
    blocked_domains: Sequence[str] = (),
) -> Tuple[List[HistoryItem], List[str]]:
    """Read every profile, returning all rows plus a warning per failed profile."""
    if not browsers:
        return [], []

    if len(browsers) == 1:
        # A pool would cost more than it saves for a single profile, which is
        # the common case for anyone who searches one browser.
        _, items, warning = _read_profile(browsers[0], search_term, limit, blocked_domains)
        return items, [warning] if warning else []

    workers = min(len(browsers), os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        reads = list(
            pool.map(
                lambda browser: _read_profile(browser, search_term, limit, blocked_domains),
                browsers,
            )
        )

    items: List[HistoryItem] = []
    warnings: List[str] = []
    for _name, profile_items, warning in reads:
        items.extend(profile_items)
        if warning:
            warnings.append(warning)
    return items, warnings


def newest_unique(items: Sequence[HistoryItem], limit: int) -> List[HistoryItem]:
    """One row per URL -- the most recent one -- newest first, at most `limit`.

    The same URL can appear in several profiles (or several times in one, when
    a search matches a URL on more than one visit), and the newest visit is the
    one worth showing.
    """
    newest_by_url = {}
    for item in items:
        known = newest_by_url.get(item.url)
        if known is None or item.normalized_time() > known.normalized_time():
            newest_by_url[item.url] = item

    ordered = sorted(newest_by_url.values(), key=lambda item: item.normalized_time(), reverse=True)
    return ordered[:limit]
