# tools/favicon_report.py
"""Why did that site get no icon? Answer it from the real profile.

Flow shows the icon the plugin hands it. The plugin reads the browser's own
icon store, so when a site's icon appears in the browser but not in Flow, the
chain between the two has broken in one of a few places:

    history row  ->  icon_mapping row  ->  favicon_bitmaps row  ->  image data

This walks that chain for a URL and says which link is missing. It reads only
copies, exactly the way the plugin does, so what it reports is what the plugin
sees -- including a stale copy, if that is the problem.

    python tools/favicon_report.py                      # the newest 25 rows
    python tools/favicon_report.py https://example.com/ # one address, in detail

Nothing here is sent anywhere and nothing is printed that identifies the
machine. It does print browsing history, though, so run it locally: never in
CI, where the log is public.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"
sys.path.insert(0, str(PLUGIN_DIR))

import browsers  # noqa: E402
import favicons  # noqa: E402

DEFAULT_ROWS = 25


class FakeItem:
    """The only two things the icon store asks of a result."""

    def __init__(self, url, browser):
        self.url = url
        self.browser = browser


def profiles():
    """Every profile on this machine that the plugin would consider."""
    found = []
    for name, base in sorted(browsers.CHROMIUM_PROFILE_BASES.items()):
        for history in browsers._get_chromium_profiles(base, "History"):
            found.append((name, history.parent, "chromium", "History"))
    for name, base in sorted(browsers.FIREFOX_BASES.items()):
        for history in browsers._get_firefox_profiles(base, "places.sqlite"):
            found.append((name, history.parent, "unix_us", "places.sqlite"))
    for name, history in sorted(browsers.FIXED_PATHS.items()):
        found.append((name, history.parent, "chromium", history.name))
    return found


def open_copy(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def newest_urls(history_copy: str, chromium: bool, limit: int):
    """The URLs the plugin would show, newest first, from one profile."""
    try:
        connection = open_copy(history_copy)
    except sqlite3.Error:
        return []
    try:
        if chromium:
            rows = connection.execute(
                "SELECT url FROM urls ORDER BY last_visit_time DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT url FROM moz_places WHERE last_visit_date IS NOT NULL"
                " ORDER BY last_visit_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
    return [row[0] for row in rows]


def in_history(history_copy: str, chromium: bool, url: str) -> str:
    """Whether this profile's history holds the URL at all."""
    try:
        connection = open_copy(history_copy)
    except sqlite3.Error:
        return "unreadable"
    try:
        if chromium:
            found = connection.execute("SELECT 1 FROM urls WHERE url = ? LIMIT 1", (url,))
        else:
            found = connection.execute(
                "SELECT 1 FROM moz_places WHERE url = ? LIMIT 1", (url,)
            )
        return "yes" if found.fetchone() else "no"
    except sqlite3.Error:
        return "unreadable"
    finally:
        connection.close()


def describe_data(data: bytes) -> str:
    if not data:
        return "empty"
    extension = favicons.sniff_extension(data)
    head = data[:8]
    shown = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    kind = "not an image Flow can draw" if extension is None else extension
    return f"{len(data)} bytes, magic {head[:4].hex()} '{shown}', {kind}"


def chain(icons_copy, chromium: bool, url: str, verbose: bool):
    """Walk history -> mapping -> bitmap -> data for one URL.

    Returns (verdict, rows) where rows are human-readable findings.
    """
    connection = open_copy(icons_copy)
    rows = []
    try:
        table = "icon_mapping" if chromium else "moz_pages_w_icons"
        id_column = "icon_id" if chromium else "id"

        exact = connection.execute(
            f"SELECT {id_column} FROM {table} WHERE page_url = ?", (url,)
        ).fetchall()
        host_ids = []
        for low, high in favicons._host_ranges(url):
            host_ids += connection.execute(
                f"SELECT page_url, {id_column} FROM {table}"
                " WHERE page_url >= ? AND page_url < ?",
                (low, high),
            ).fetchall()

        if verbose:
            rows.append(f"  exact mapping rows: {len(exact)}")
            rows.append(f"  rows for the whole site: {len(host_ids)}")
            for page_url, icon_id in host_ids[:5]:
                rows.append(f"    {page_url[:80]} -> icon {icon_id}")

        icon_ids = sorted(
            {row[0] for row in exact if row[0] is not None}
            | {row[1] for row in host_ids if row[1] is not None}
        )
        if not icon_ids:
            return "no icon_mapping row for this site", rows

        if chromium:
            bitmaps = connection.execute(
                "SELECT icon_id, width, height, image_data FROM favicon_bitmaps"
                f" WHERE icon_id IN ({', '.join('?' * len(icon_ids))})",
                icon_ids,
            ).fetchall()
        else:
            bitmaps = connection.execute(
                "SELECT id, width, 0, data FROM moz_icons"
                f" WHERE id IN ({', '.join('?' * len(icon_ids))})",
                icon_ids,
            ).fetchall()

        if not bitmaps:
            return "mapping points at an icon with no stored bitmap", rows

        usable = 0
        for icon_id, width, height, data in bitmaps:
            if verbose:
                rows.append(f"    icon {icon_id} @ {width}x{height}: {describe_data(data)}")
            if data and favicons.sniff_extension(data):
                usable += 1
        if not usable:
            return "bitmaps exist but none holds an image Flow can draw", rows
        return f"icon available ({usable} usable bitmap(s))", rows
    except sqlite3.Error as error:
        return f"could not read the icon store ({error})", rows
    finally:
        connection.close()


def report(urls, limit=DEFAULT_ROWS):
    cache = Path(tempfile.mkdtemp(prefix="bh-report-"))
    any_profile = False

    for name, directory, timestamp_type, db_file in profiles():
        try:
            browser = browsers.Browser(
                name=name,
                cache_dir=cache,
                timestamp_type=timestamp_type,
                custom_path=directory,
                db_file=db_file,
            )
        except (OSError, ValueError):
            continue
        any_profile = True
        print(f"\n=== {name} — {directory.name} ===")

        try:
            history_copy = browser._copy_database()
        except OSError as error:
            print(f"  history unreadable: {error}")
            continue

        chromium = browser.engine == browsers.CHROMIUM_ENGINE
        wanted = urls or newest_urls(history_copy, chromium, limit)
        if not wanted:
            print("  no history rows")
            continue

        source = browser.icons_database()
        if source is None:
            print("  no icon store in this profile")
            continue
        wal = Path(str(source) + "-wal")
        print(
            f"  icon store: {source.stat().st_size} bytes"
            + (f" (+{wal.stat().st_size} bytes still in the write-ahead log)" if wal.exists() else "")
        )

        try:
            icons_copy = browser.copy_icons_database()
        except OSError as error:
            print(f"  icon store unreadable: {error}")
            continue

        # What the plugin itself would hand Flow, cache and all.
        store = favicons.FaviconStore(cache / "favicons-probe")
        resolved = store.resolve([FakeItem(url, browser) for url in wanted])

        counts = {}
        for url in wanted:
            verdict, rows = chain(icons_copy, chromium, url, verbose=bool(urls))
            if in_history(history_copy, chromium, url) == "no":
                verdict = "not in this profile's history (another profile has it)"
            counts[verdict] = counts.get(verdict, 0) + 1
            print(f"  {url[:78]}")
            for row in rows:
                print(row)
            print(f"    -> {verdict}")
            path = resolved.get(url)
            if path:
                print(f"    -> plugin hands Flow {Path(path).name} ({Path(path).stat().st_size} bytes)")
            else:
                print("    -> plugin hands Flow nothing")

        if not urls:
            print("  summary:")
            for verdict, count in sorted(counts.items(), key=lambda item: -item[1]):
                print(f"    {count:3d}  {verdict}")

    if not any_profile:
        print("No browser profiles found on this machine.")
    print(
        "\nRead it as: a link is missing where the first line differs from the\n"
        "line above it. 'no icon_mapping row for this site' means the browser\n"
        "never stored an icon for that site in this profile -- the icon you see\n"
        "in the browser's history page was fetched from the site at that moment,\n"
        "not kept on disk. 'none holds an image Flow can draw' means the bytes\n"
        "are not PNG/ICO/GIF (an SVG, typically)."
    )


def main(argv):
    urls = [argument for argument in argv[1:] if not argument.startswith("-")]
    limit = DEFAULT_ROWS
    for argument in argv[1:]:
        if argument.startswith("--rows="):
            limit = int(argument.split("=", 1)[1])
    report(urls, limit=limit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
