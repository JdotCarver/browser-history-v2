#!/usr/bin/env python3
"""Measure how well the current matcher answers the searches you actually type.

The plugin matches by substring: every space-separated word must appear inside
the title or the URL. That is fast and predictable, but it cannot answer a
typo, an abbreviation, or words typed out of order across the two fields.

Before changing the matcher, this measures what the current one misses. Run it
against your real history, with a list of the searches you care about:

    python tools/perf/match_report.py probes.json

where probes.json looks like:

    [
      {"query": "level1linux", "expected": "youtube.com/@Level1Linux"},
      {"query": "flox", "expected": "github.com/Flox"}
    ]

`expected` is any fragment of the URL you would want to open; if omitted, the
probe only reports how many results the query returns.

For each probe it reports one of:

    found       the expected page is in the results (and at which rank)
    filtered    the page is in history and would match, but is not shown --
                a limit or ordering question, not a matching one
    fuzzy       the page is in history, but only a looser (subsequence) match
                could reach it: this is what a new matcher would win
    unmatchable the page is in history but the query does not describe it in
                any reasonable way -- the expectation, not the matcher, is wrong
    absent      the page is not in the scanned history at all

Add --typos to answer the other half of the question: how often a single
mistyped character still finds the page.

    python tools/perf/match_report.py probes.json --typos
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin"))

import browsers  # noqa: E402
from results import newest_unique, read_profiles  # noqa: E402

# How deep to scan when deciding whether a page is in history at all. The
# plugin itself only ever reads what it can display; this is the wider net used
# to prove a miss is about matching rather than about depth.
DEFAULT_SCAN = 20000


def load_browsers(profile_mode: str = "All Profiles") -> List[browsers.Browser]:
    """Every supported profile, discovered the way the plugin does."""
    import tempfile

    cache_dir = Path(tempfile.mkdtemp(prefix="bh-match-cache-"))
    found: List[browsers.Browser] = []
    names = (
        list(browsers.CHROMIUM_PROFILE_BASES)
        + list(browsers.FIREFOX_BASES)
        + list(browsers.FIXED_PATHS)
    )
    for name in names:
        try:
            if profile_mode == "All Profiles":
                found.extend(browsers.get_all_profiles(name, cache_dir))
            else:
                single = browsers.get(name, cache_dir, profile_last_updated=True)
                if single:
                    found.append(single)
        except Exception:
            continue

    unique: List[browsers.Browser] = []
    seen = set()
    for browser in found:
        key = str(browser.database_path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(browser)
    return unique


def is_subsequence(needle: str, haystack: str) -> bool:
    """Would fzf-style matching reach this? Characters in order, gaps allowed."""
    position = 0
    for character in haystack:
        if position < len(needle) and character == needle[position]:
            position += 1
    return position == len(needle)


def matches_current(query: str, title: str, url: str) -> bool:
    """What the plugin does today: every word a substring of title or URL."""
    haystack = f"{title} {url}".lower()
    return all(term in haystack for term in query.lower().split())


def matches_loosely(query: str, title: str, url: str) -> bool:
    """A subsequence match on either field, ignoring the spaces."""
    needle = "".join(query.lower().split())
    if not needle:
        return True
    return is_subsequence(needle, title.lower()) or is_subsequence(needle, url.lower())


def search(profiles, query: str, limit: int = 25) -> List:
    items, _warnings = read_profiles(profiles, query, min(limit, 500), [])
    return newest_unique(items, limit)


def scan(profiles, per_profile: int = DEFAULT_SCAN) -> List[Tuple[str, str]]:
    """The newest (url, title) pairs, as a wide net for 'is it there at all'."""
    rows: List[Tuple[str, str]] = []
    for profile in profiles:
        try:
            for item in profile.history(search_term="", limit=per_profile):
                rows.append((item.url, item.title))
        except Exception:
            continue
    return rows


def describe(probe: dict, profiles, history: Optional[Sequence[Tuple[str, str]]]) -> dict:
    query = probe["query"]
    expected = (probe.get("expected") or "").lower()

    results = search(profiles, query)
    rank = None
    for position, item in enumerate(results, start=1):
        if expected and expected in item.url.lower():
            rank = position
            break

    outcome = {
        "query": query,
        "expected": probe.get("expected", ""),
        "results": len(results),
        "rank": rank,
        "verdict": "found" if rank else ("unchecked" if not expected else ""),
        "top": [item.title for item in results[:3]],
    }
    if not expected or rank:
        return outcome

    if history is None:
        return outcome

    for url, title in history:
        if expected in url.lower():
            if matches_current(query, title, url):
                outcome["verdict"] = "filtered"
            elif matches_loosely(query, title, url):
                outcome["verdict"] = "fuzzy"
            else:
                outcome["verdict"] = "unmatchable"
            outcome["where"] = url
            return outcome

    outcome["verdict"] = "absent"
    return outcome


def typo_variants(query: str) -> List[Tuple[str, str]]:
    """One dropped character and one transposition, per word."""
    variants: List[Tuple[str, str]] = []
    words = query.split()
    if not words:
        return variants
    word = words[0]
    if len(word) > 3:
        middle = len(word) // 2
        variants.append((word[:middle] + word[middle + 1 :], "dropped a letter"))
    if len(words) == 1 and len(word) > 2:
        variants.append((word[:2] + word[2:4][::-1] + word[4:], "two letters swapped"))
    return variants


def typo_report(probes: Sequence[dict], profiles) -> List[dict]:
    rows: List[dict] = []
    for probe in probes:
        expected = (probe.get("expected") or "").lower()
        if not expected:
            continue
        for variant, description in typo_variants(probe["query"]):
            if variant == probe["query"]:
                continue
            results = search(profiles, variant)
            found = any(expected in item.url.lower() for item in results)
            rows.append(
                {
                    "query": probe["query"],
                    "typed": variant,
                    "change": description,
                    "found": found,
                }
            )
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("probes", help="JSON file: [{'query': ..., 'expected': ...}]")
    parser.add_argument("--scope", default="All Profiles", choices=["All Profiles", "Active Profile"])
    parser.add_argument("--scan", type=int, default=DEFAULT_SCAN, help="history rows per profile to scan for the proof")
    parser.add_argument("--typos", action="store_true", help="also measure tolerance to a single mistyped character")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    probes = json.loads(Path(args.probes).read_text(encoding="utf-8"))
    profiles = load_browsers(args.scope)
    if not profiles:
        print("No browser profiles were found. Run this on the machine with the history.")
        return 1

    print(f"profiles: {len(profiles)} ({', '.join(p.name for p in profiles)})")
    needs_scan = any(p.get("expected") for p in probes)
    history = scan(profiles, args.scan) if needs_scan else None
    if history is not None:
        print(f"scanned: {len(history)} recent rows, as the proof of presence")

    outcomes = [describe(probe, profiles, history) for probe in probes]
    typos = typo_report(probes, profiles) if args.typos else []

    if args.json:
        print(json.dumps({"outcomes": outcomes, "typos": typos}, indent=2))
        return 0

    print()
    print(f"{'query':<24}{'rank':>5}{'results':>9}  verdict")
    print("-" * 72)
    for outcome in outcomes:
        rank = outcome["rank"] if outcome["rank"] else "-"
        print(
            f"{outcome['query'][:23]:<24}{str(rank):>5}{outcome['results']:>9}  {outcome['verdict']}"
        )
        if outcome["verdict"] in ("filtered", "fuzzy", "unmatchable") and outcome.get("where"):
            print(f"    in history: {outcome['where'][:70]}")

    if typos:
        hit = sum(1 for row in typos if row["found"])
        print()
        print(f"typos: {hit}/{len(typos)} mistyped searches still found the page")
        for row in typos:
            mark = "ok  " if row["found"] else "miss"
            print(f"  {mark} '{row['typed']}' ({row['change']}) -> {row['query']}")

    print()
    print("verdicts: found / filtered (limit or ordering) / fuzzy (needs a looser matcher)")
    print("          unmatchable (the query does not describe the page) / absent")
    counts = {}
    for outcome in outcomes:
        counts[outcome["verdict"]] = counts.get(outcome["verdict"], 0) + 1
    print("summary:", ", ".join(f"{key} {value}" for key, value in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
