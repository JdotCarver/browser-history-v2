# plugin/favicons.py
"""Site icons, taken from the browser's own favicon store.

Nothing here touches the network. Chromium keeps every icon it has ever
fetched in `<profile>/Favicons`, and Firefox keeps the same thing in
`<profile>/favicons.sqlite`, so the icon for a history row is already on disk.

Icons are written once per distinct image, named by the hash of their bytes:

  * Flow Launcher caches icons by path, so a content-addressed name can never
    show a stale image;
  * every visit to the same site shares one file;
  * a JSON index remembers which file belongs to which URL, so a warm keystroke
    never opens the icon database at all.

The index is the only thing written per keystroke, and only when it changed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

# Formats Flow Launcher can draw. Firefox stores some icons as SVG, which is
# skipped: a wrong icon is worse than the history glyph it falls back to.
MAGIC_EXTENSIONS = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\x00\x00\x01\x00", ".ico"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

# A list row is small; 32px is crisp on a scaled display without being big.
PREFERRED_WIDTH = 32

# The index is capped so a cache left running for years cannot grow without
# bound. Oldest entries are dropped first and their files deleted.
MAX_INDEX_ENTRIES = 2000
EVICT_FRACTION = 5  # drop a fifth at a time, not one per keystroke

WEB_SCHEMES = ("https", "http")


def sniff_extension(data: bytes) -> Optional[str]:
    """The file extension for an image Flow Launcher can display, or None."""
    for magic, extension in MAGIC_EXTENSIONS:
        if data.startswith(magic):
            return extension
    return None


def _host(url: str) -> str:
    """The site part of a URL, or '' when it has none."""
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _alternate_hosts(url: str) -> List[str]:
    """The same site spelled the other way, when `www.` is the only difference.

    Browsers file the icon under the page URL that loaded it, and a site that
    redirects between `example.com` and `www.example.com` leaves the icon on one
    spelling and the history row on the other. This is the only rewriting that
    is safe without a table of sites: the two hosts are the same origin in
    practice, and if no icon is filed there nothing happens.
    """
    host = _host(url)
    if not host:
        return []
    if host.startswith("www."):
        return [host[len("www."):]]
    return [f"www.{host}"]


def _rewrite_host(url: str, host: str) -> str:
    """The URL with its host replaced, leaving path and query alone."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def _host_ranges(url: str) -> List[Tuple[str, str]]:
    """Bounds that select every icon stored for this URL's site.

    A range rather than a LIKE: 'LIKE' cannot use the page_url index unless
    case-sensitive matching is switched on, and that turns a 25-URL lookup into
    a scan of every icon in the profile.
    """
    host = _host(url)
    if not host:
        return []
    ranges = []
    for scheme in WEB_SCHEMES:
        prefix = f"{scheme}://{host}/"
        # Everything beneath a prefix sorts between it and the next character.
        ranges.append((prefix, prefix[:-1] + chr(ord("/") + 1)))
    return ranges


def _pick_best(rows: Sequence[Tuple[str, bytes, int]]) -> Dict[str, bytes]:
    """One image per URL: the closest to PREFERRED_WIDTH."""
    best: Dict[str, Tuple[float, bytes]] = {}
    for url, data, width in rows:
        if not url or not data or sniff_extension(data) is None:
            continue
        width = width or 0
        # 0 means "size not recorded"; rank it behind any known size.
        distance = abs(width - PREFERRED_WIDTH) if width else PREFERRED_WIDTH * 4
        if url not in best or distance < best[url][0]:
            best[url] = (distance, data)
    return {url: data for url, (_distance, data) in best.items()}


def _open(database: str):
    try:
        # A private connection opened read-only: the copy is ours, and a URI
        # keeps SQLite from creating a journal file next to it.
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _select(connection, sql: str, parameters=()) -> List[Tuple]:
    """Run one query, treating any failure as 'no rows'.

    An older or unexpected schema must cost an icon, never the whole search.
    """
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.Error:
        return []


def _in(column: str, values: Sequence) -> Tuple[str, List]:
    return f"{column} IN ({', '.join('?' * len(values))})", list(values)


def _pages_for(
    connection, table: str, id_column: str, exact: Sequence[str], sites: Sequence[str]
) -> Dict[str, List[int]]:
    """Ids recorded for the pages asked about: exact URLs first, then whole sites.

    Every query here is a seek on the page_url index. Two shapes of query are
    deliberately avoided: a LIKE (which cannot use that index without
    case-sensitive matching) and one big OR across all the sites (which the
    planner answers with a scan of the whole icon table -- 25 ms against 0.2 ms
    for the same lookups done one site at a time).
    """
    pages: Dict[str, List[int]] = {}

    if exact:
        where, parameters = _in("page_url", exact)
        for page_url, page_id in _select(
            connection, f"SELECT page_url, {id_column} FROM {table} WHERE {where}", parameters
        ):
            if page_id is not None:
                pages.setdefault(page_url, []).append(page_id)

    for url in sites:
        for low, high in _host_ranges(url):
            for page_url, page_id in _select(
                connection,
                f"SELECT page_url, {id_column} FROM {table}"
                " WHERE page_url >= ? AND page_url < ?",
                (low, high),
            ):
                if page_id is not None:
                    pages.setdefault(page_url, []).append(page_id)
    return pages


def _chromium_lookup(connection, exact: Sequence[str], sites: Sequence[str]) -> List[Tuple]:
    """(page_url, image_data, width) for the pages asked about.

    Staged rather than joined: the planner is free to drive a join from either
    table, and driving from the bitmaps costs a scan of every icon in the
    profile. Each stage here is an index seek on its own table.
    """
    pages = _pages_for(connection, "icon_mapping", "icon_id", exact, sites)
    icon_ids = sorted({icon for found in pages.values() for icon in found})
    if not icon_ids:
        return []

    condition, parameters = _in("icon_id", icon_ids)
    bitmaps: Dict[int, Tuple[bytes, int]] = {}
    for icon_id, image_data, width in _select(
        connection,
        f"SELECT icon_id, image_data, width FROM favicon_bitmaps WHERE {condition}",
        parameters,
    ):
        if image_data and (icon_id not in bitmaps or (width or 0) > bitmaps[icon_id][1]):
            bitmaps[icon_id] = (image_data, width or 0)

    return [
        (page_url, *bitmaps[icon_id])
        for page_url, found in pages.items()
        for icon_id in found
        if icon_id in bitmaps
    ]


def _firefox_lookup(connection, exact: Sequence[str], sites: Sequence[str]) -> List[Tuple]:
    """The same staged lookup against Firefox's three tables."""
    pages = _pages_for(connection, "moz_pages_w_icons", "id", exact, sites)
    if not pages:
        return []

    page_ids = {page_id for found in pages.values() for page_id in found}
    condition, parameters = _in("page_id", sorted(page_ids))
    links = _select(
        connection, f"SELECT page_id, icon_id FROM moz_icons_to_pages WHERE {condition}", parameters
    )
    if not links:
        return []

    condition, parameters = _in("id", sorted({icon_id for _page, icon_id in links}))
    icons: Dict[int, Tuple[bytes, int]] = {}
    for icon_id, data, width in _select(
        connection, f"SELECT id, data, width FROM moz_icons WHERE {condition}", parameters
    ):
        if data:
            icons[icon_id] = (data, width or 0)

    return [
        (page_url, *icons[icon_id])
        for page_url, found in pages.items()
        for page_id in found
        for known_page, icon_id in links
        if known_page == page_id and icon_id in icons
    ]


def lookup_icons(
    database: str, chromium: bool, exact: Sequence[str], sites: Sequence[str]
) -> Dict[str, bytes]:
    """One image per page URL, preferring the size nearest the preferred one."""
    connection = _open(database)
    if connection is None:
        return {}
    try:
        if chromium:
            rows = _chromium_lookup(connection, exact, sites)
        else:
            rows = _firefox_lookup(connection, exact, sites)
    finally:
        connection.close()
    return _pick_best(rows)


class FaviconStore:
    """Resolves result URLs to cached icon files."""

    def __init__(self, cache_dir: Path, browsers: Sequence = ()) -> None:
        self.directory = Path(cache_dir) / "favicons"
        self.index_path = self.directory / "index.json"
        self.browsers = list(browsers)
        self._index: Optional[Dict[str, str]] = None
        self._dirty = False

    # -- the persistent index -------------------------------------------------

    def _load_index(self) -> Dict[str, str]:
        if self._index is not None:
            return self._index

        index: Dict[str, str] = {}
        try:
            loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                index = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            # A missing or damaged index costs one lookup per icon, no more.
            index = {}
        self._index = index
        return index

    def _save_index(self, index: Dict[str, str]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if len(index) > MAX_INDEX_ENTRIES:
                self._evict(index)
            self.index_path.write_text(json.dumps(index), encoding="utf-8")
        except OSError:
            pass  # A cache that cannot be written is still a working plugin.
        self._dirty = False

    def _evict(self, index: Dict[str, str]) -> None:
        """Drop the oldest entries and their files, keeping the cache bounded."""
        excess = max(1, len(index) // EVICT_FRACTION)
        for url in list(index)[:excess]:
            name = index.pop(url)
            try:
                (self.directory / name).unlink()
            except OSError:
                pass

    # -- resolving ------------------------------------------------------------

    def resolve(self, items: Sequence) -> Dict[str, str]:
        """Map the URLs of these results to icon files, where one is known."""
        wanted: Dict[str, object] = {}
        for item in items:
            url = getattr(item, "url", "")
            if url and url not in wanted:
                wanted[url] = item
        if not wanted:
            return {}

        index = self._load_index()
        resolved: Dict[str, str] = {}
        pending: List[str] = []

        for url in wanted:
            name = index.get(url)
            if name:
                path = self.directory / name
                if path.exists():
                    resolved[url] = str(path)
                    continue
                # The file was collected or deleted: forget it and look again.
                del index[url]
                self._dirty = True
            pending.append(url)

        if pending:
            self._look_up(index, resolved, pending, wanted)

        if self._dirty:
            self._save_index(index)
        return resolved

    def _look_up(self, index, resolved, pending, wanted) -> None:
        grouped: Dict[int, Tuple[object, List[str]]] = {}
        for url in pending:
            browser = getattr(wanted[url], "browser", None)
            if browser is None:
                continue
            key = id(browser)
            if key not in grouped:
                grouped[key] = (browser, [])
            grouped[key][1].append(url)

        for browser, urls in grouped.values():
            try:
                database = browser.copy_icons_database()
            except (OSError, AttributeError, FileNotFoundError):
                # No icon store for this profile (older Firefox, a browser that
                # does not keep one): the results still show.
                continue
            if not database:
                continue

            chromium = browser.timestamp_type == "chromium"
            known = lookup_icons(database, chromium, urls, [])

            # A page the browser never stored an icon for borrows one from the
            # same site -- filed under the URL being shown, not under the
            # neighbour it came from. This only runs when it can still help.
            by_host: Dict[str, bytes] = {}
            for page_url, data in known.items():
                by_host.setdefault(_host(page_url), data)
            missing = [url for url in urls if url not in known and _host(url) not in by_host]
            if missing:
                for page_url, data in lookup_icons(database, chromium, [], missing).items():
                    by_host.setdefault(_host(page_url), data)

            # Still nothing: the icon may be filed under the other spelling of
            # the same site (`www.` or not). One lookup for all of them, so an
            # iconless list still opens the icon store once.
            unresolved = [url for url in urls if url not in known and _host(url) not in by_host]
            if unresolved:
                addresses = {}
                for url in unresolved:
                    for alternate in _alternate_hosts(url):
                        rewritten = _rewrite_host(url, alternate)
                        if rewritten:
                            addresses[url] = (alternate, rewritten)
                if addresses:
                    found = lookup_icons(
                        database, chromium, [], [rewritten for _alternate, rewritten in addresses.values()]
                    )
                    for page_url, icon in found.items():
                        for url, (alternate, _rewritten) in addresses.items():
                            if _host(page_url) == alternate:
                                by_host.setdefault(_host(url), icon)

            for url in urls:
                data = known.get(url) or by_host.get(_host(url))
                if data is None:
                    continue
                name = self._store(data)
                if not name:
                    continue
                index[url] = name
                resolved[url] = str(self.directory / name)
                self._dirty = True

    def _store(self, data: bytes) -> Optional[str]:
        """Write an icon once, under the hash of its bytes."""
        extension = sniff_extension(data)
        if extension is None:
            return None

        name = hashlib.sha256(data).hexdigest()[:32] + extension
        path = self.directory / name
        if path.exists():
            return name

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Write to a temporary name first: a half-written icon would be
            # cached by Flow Launcher as a broken image forever.
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError:
            return None
        return name
