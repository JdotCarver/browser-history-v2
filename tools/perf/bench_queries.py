# tools/perf/bench_queries.py
"""A/B benchmark for the history search: the published code versus the working tree.

Usage
-----
    python tools/perf/bench_queries.py                    # both, side by side
    python tools/perf/bench_queries.py --profiles 4       # heavier profile set
    python tools/perf/bench_queries.py --impl current     # one side only
    python tools/perf/bench_queries.py --baseline-ref origin/main

Each side runs in its own subprocess, because that is also how Flow Launcher
invokes the plugin (a fresh interpreter per keystroke) and because it keeps two
different versions of `browsers.py` from sharing module state.

Both sides are measured through the plugin's real aggregation path: read every
profile, then keep the newest row per URL. The only thing that differs is how
the rows are read out of SQLite, which is the point of the comparison.

The databases are synthetic (tests/history_fixtures.py) and thrown away
afterwards; nothing is written inside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugin"
FIXTURES_DIR = REPO_ROOT / "tests"

#: Queries that represent what a user's keystrokes actually look like.
SCENARIOS = [
    ("empty", "", "the plugin's idle listing of recent history"),
    ("common", "youtube", "a word that matches many rows"),
    ("medium", "kernel", "a word that matches a few rows"),
    ("multi-word", "python sqlite", "two words, both required"),
    ("absent", "zzqq", "still typing: matches nothing at all"),
]


#: How evenly history is spread over the modelled five years.
#: `flat` is the pessimistic case for a recency ladder (every window is as dense
#: as the whole table); `skewed` models a real profile, where recent months hold
#: most of the visits.
DISTRIBUTIONS = {"flat": 1.0, "skewed": 3.0}


def build_fixture(name: str, url_count: int, with_time_index: bool, age_skew: float) -> Path:
    """Create (or reuse) one synthetic database under the temporary root."""
    sys.path.insert(0, str(FIXTURES_DIR))
    import history_fixtures

    path = FIXTURE_ROOT / name
    if path.exists():
        return path
    if name.startswith("chromium"):
        history_fixtures.build_chromium_history(
            path,
            url_count,
            with_time_index=with_time_index,
            seed=abs(hash(name)) % 1000,
            age_skew=age_skew,
        )
    else:
        history_fixtures.build_firefox_places(
            path,
            url_count,
            with_time_index=with_time_index,
            seed=abs(hash(name)) % 1000,
            age_skew=age_skew,
        )
    return path


def profile_directories(
    profiles: int, urls_per_profile: int, distribution: str, firefox_profiles: int = 0
) -> list[tuple[str, str, Path]]:
    """Lay out browser profile folders, each holding a real database file."""
    layout = []
    for index in range(profiles):
        folder = FIXTURE_ROOT / "profiles" / f"chromium{index}"
        folder.mkdir(parents=True, exist_ok=True)
        source = build_fixture(
            f"chromium_{index}",
            urls_per_profile,
            with_time_index=True,
            age_skew=DISTRIBUTIONS[distribution],
        )
        target = folder / "History"
        if not target.exists():
            target.write_bytes(source.read_bytes())
        layout.append((f"chromium{index}", "chromium", folder))

    for index in range(firefox_profiles):
        folder = FIXTURE_ROOT / "profiles" / f"firefox{index}"
        folder.mkdir(parents=True, exist_ok=True)
        source = build_fixture(
            f"firefox_{index}",
            urls_per_profile,
            with_time_index=True,
            age_skew=DISTRIBUTIONS[distribution],
        )
        target = folder / "places.sqlite"
        if not target.exists():
            target.write_bytes(source.read_bytes())
        layout.append((f"firefox{index}", "firefox", folder))
    return layout


def measure(impl: str, layout, scenarios, repeats: int) -> dict:
    """Time each scenario through this implementation; run inside the subprocess."""
    if impl == "baseline":
        import browsers as browser_module

        def make_browser(name, kind, folder):
            if kind == "firefox":
                return browser_module.Browser(
                    name,
                    browser_module.FIREFOX_QUERY,
                    CACHE_DIR,
                    "unix_us",
                    custom_path=folder,
                    db_file="places.sqlite",
                )
            return browser_module.Browser(
                name,
                browser_module.CHROMIUM_QUERY,
                CACHE_DIR,
                "chromium",
                custom_path=folder,
                db_file="History",
            )

        def search(browsers, term, limit):
            items = []
            for browser in browsers:
                items.extend(
                    browser.history(search_term=term, limit=limit, blocked_domains=[])
                )
            newest = {}
            for item in items:
                known = newest.get(item.url)
                if known is None or item.normalized_time() > known.normalized_time():
                    newest[item.url] = item
            return sorted(
                newest.values(), key=lambda item: item.normalized_time(), reverse=True
            )[:limit]

    else:
        import browsers as browser_module
        import results

        def make_browser(name, kind, folder):
            if kind == "firefox":
                return browser_module.Browser(
                    name, CACHE_DIR, "unix_us", custom_path=folder, db_file="places.sqlite"
                )
            return browser_module.Browser(
                name, CACHE_DIR, "chromium", custom_path=folder, db_file="History"
            )

        def search(browsers, term, limit):
            items, _warnings = results.read_profiles(browsers, term, limit, [])
            return results.newest_unique(items, limit)

    browsers = [make_browser(name, kind, folder) for name, kind, folder in layout]
    limit = 500

    timings = {}
    for key, term, _description in scenarios:
        search(browsers, term, limit)  # warm the copied databases and the page cache
        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            found = len(search(browsers, term, limit))
            samples.append((time.perf_counter() - started) * 1000)
        timings[key] = {"median_ms": round(statistics.median(samples), 1), "results": found}
    return timings


CHROMIUM_EPOCH_OFFSET_SECONDS = 11_644_473_600
FIXTURE_ROOT = Path(tempfile.gettempdir()) / "browser-history-perf"
CACHE_DIR = FIXTURE_ROOT / "cache"


def run_side(impl: str, baseline_source: Path | None, args) -> dict:
    """Re-enter this script with a PYTHONPATH that selects one implementation."""
    environment = dict(os.environ)
    paths = [str(PLUGIN_DIR)]
    if impl == "baseline":
        paths = [str(baseline_source)]
        # The published module reads these at import time and crashes without them.
        environment.setdefault("LOCALAPPDATA", str(FIXTURE_ROOT / "appdata"))
        environment.setdefault("APPDATA", str(FIXTURE_ROOT / "appdata"))
    environment["PYTHONPATH"] = os.pathsep.join(paths + [environment.get("PYTHONPATH", "")])

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--impl",
        impl,
        "--profiles",
        str(args.profiles),
        "--urls",
        str(args.urls),
        "--repeats",
        str(args.repeats),
        "--firefox",
        str(args.firefox),
        "--distribution",
        args.distribution,
        "--json",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, env=environment)
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"{impl} benchmark failed")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def extract_baseline(ref: str) -> tuple[Path, str]:
    """Write the published `browsers.py` into a throwaway import directory."""
    resolved = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    source = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{ref}:plugin/browsers.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    directory = Path(tempfile.mkdtemp(prefix="browser-history-baseline-"))
    (directory / "browsers.py").write_text(source)
    return directory, resolved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--impl", choices=["baseline", "current"], help="run one side only")
    parser.add_argument("--baseline-ref", default="origin/main", help="git ref of the published code")
    parser.add_argument("--profiles", type=int, default=4, help="how many browser profiles to fake")
    parser.add_argument("--urls", type=int, default=200_000, help="distinct URLs per profile")
    parser.add_argument("--firefox", type=int, default=0, help="how many Firefox profiles to fake")
    parser.add_argument("--repeats", type=int, default=7, help="shots per scenario")
    parser.add_argument(
        "--distribution",
        choices=sorted(DISTRIBUTIONS),
        default="skewed",
        help="how evenly the fake history is spread over time",
    )
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    global FIXTURE_ROOT, CACHE_DIR
    FIXTURE_ROOT = FIXTURE_ROOT / args.distribution
    CACHE_DIR = FIXTURE_ROOT / "cache"
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    layout = profile_directories(args.profiles, args.urls, args.distribution, args.firefox)

    if args.impl:
        print(json.dumps(measure(args.impl, layout, SCENARIOS, args.repeats)))
        return 0

    baseline_dir, baseline_commit = extract_baseline(args.baseline_ref)
    print(f"hardware: {os.cpu_count()} cores, {sys.platform}, python {sys.version.split()[0]}")
    print(f"fixture: {args.profiles} Chromium + {args.firefox} Firefox profiles"
          f" x {args.urls:,} URLs each ({args.distribution} history)")
    print(f"published code: {baseline_commit} ({args.baseline_ref})")
    print()

    baseline = run_side("baseline", baseline_dir, args)
    current = run_side("current", None, args)

    print(f"{'scenario':<12}{'query':<16}{'published':>12}{'working tree':>15}{'speedup':>10}")
    print("-" * 66)
    for key, term, _description in SCENARIOS:
        before = baseline[key]["median_ms"]
        after = current[key]["median_ms"]
        speedup = before / after if after else float("inf")
        shown = term if term else "(empty)"
        print(f"{key:<12}{shown:<16}{before:>9.0f} ms{after:>12.0f} ms{speedup:>9.1f}x")
    print()
    for key, term, description in SCENARIOS:
        print(f"  {key:<11} {description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
