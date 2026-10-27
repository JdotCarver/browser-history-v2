# plugin/browsers.py
"""Finding a browser's history database, and reading rows out of it."""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import time
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from history_query import FIREFOX_ENGINE, CHROMIUM_ENGINE, read_history

# Environment variables. Empty on a non-Windows machine, which simply makes
# every profile path below a relative path that does not exist: the plugin then
# reports "no browsers found" instead of failing to import, and the test suite
# can run anywhere.
LOCAL_DATA = os.getenv("LOCALAPPDATA", "")
ROAMING = os.getenv("APPDATA", "")

# Chromium profile base folders (User Data only, profile selected later)
CHROMIUM_PROFILE_BASES = {
    "chrome": Path(LOCAL_DATA, "Google", "Chrome", "User Data"),
    "edge": Path(LOCAL_DATA, "Microsoft", "Edge", "User Data"),
    "brave": Path(LOCAL_DATA, "BraveSoftware", "Brave-Browser", "User Data"),
    "brave nightly": Path(
        LOCAL_DATA, "BraveSoftware", "Brave-Browser-Nightly", "User Data"
    ),
    "vivaldi": Path(LOCAL_DATA, "Vivaldi", "User Data"),
    "arc": Path(
        LOCAL_DATA,
        "Packages",
        "TheBrowserCompany.Arc_ttt1ap7aakyb4",
        "LocalCache",
        "Local",
        "Arc",
        "User Data",
    ),
    "thorium": Path(LOCAL_DATA, "Thorium", "User Data"),
    "helium": Path(LOCAL_DATA, "imput", "Helium", "User Data"),
}

# Single fixed path browsers
FIXED_PATHS = {
    "opera": Path(ROAMING, "Opera Software", "Opera Stable", "Default", "History")
}

FIREFOX_BASES = {
    "firefox": Path(ROAMING, "Mozilla", "Firefox", "Profiles"),
    "zen": Path(ROAMING, "zen", "Profiles"),
    "floorp": Path(ROAMING, "Floorp", "Profiles"),
    "waterfox": Path(ROAMING, "Waterfox", "Profiles"),
}

CHROMIUM_EPOCH_OFFSET = 11644473600

# Resolving the computer name is a system call (a registry read on Windows) and
# the cache file name only needs it once per process.
_computer_name: Optional[str] = None


def computer_name() -> str:
    """This machine's name, for namespacing cache files to the current PC."""
    global _computer_name
    if _computer_name is None:
        _computer_name = platform.node()
    return _computer_name


WRITE_AHEAD_LOG = "-wal"


def _log_of(database: Path) -> Path:
    """The write-ahead log that belongs to this database, if any."""
    return Path(str(database) + WRITE_AHEAD_LOG)


def _newest_mtime(database: Path) -> Optional[float]:
    """The later of a database and its log: the moment its state last changed."""
    newest = None
    for candidate in (database, _log_of(database)):
        try:
            stamp = candidate.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def _size(path: Path) -> int:
    """File size, or -1 when there is no file (so 'missing' never matches 0)."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _readable(database: Path) -> bool:
    """Whether SQLite can open the copy and read its schema."""
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return False
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _discard_log(database: Path) -> None:
    """Remove a copy's log and the shared-memory file SQLite keeps beside it."""
    for suffix in (WRITE_AHEAD_LOG, WRITE_AHEAD_LOG + "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)


def _get_chromium_profiles(base: Path, db_file: str) -> List[Path]:
    if not base.exists():
        return []
    return [
        p / db_file
        for p in base.iterdir()
        if p.is_dir()
        and (p / db_file).exists()
        and (p.name == "Default" or p.name.startswith("Profile "))
    ]


def _get_firefox_profiles(base: Path, db_file: str) -> List[Path]:
    if not base.exists():
        return []
    return [p / db_file for p in base.iterdir() if p.is_dir() and (p / db_file).exists()]


class Browser:
    """Represents a single browser profile history database."""

    def __init__(
        self,
        name: str,
        cache_dir: Path,
        timestamp_type: str = "chromium",
        custom_path: Optional[Path] = None,
        db_file: str = "History",
        profile_last_updated: bool = False,
    ) -> None:
        self.name = name
        self.cache_dir = cache_dir
        self.timestamp_type = timestamp_type
        self.engine = (
            CHROMIUM_ENGINE if timestamp_type == "chromium" else FIREFOX_ENGINE
        )
        self.db_file = db_file

        # Resolve database path
        if custom_path:
            # Enforce directory-only custom path strictly
            if not isinstance(custom_path, Path):
                custom_path = Path(custom_path)
            if not custom_path.exists() or not custom_path.is_dir():
                raise FileNotFoundError(
                    f"Custom profile directory does not exist or is not a directory: '{custom_path}'"
                )
            self.database_path = custom_path / db_file
        elif name in CHROMIUM_PROFILE_BASES:
            self.database_path = self._select_chromium_profile(
                CHROMIUM_PROFILE_BASES[name], db_file, profile_last_updated
            )
        elif name in FIREFOX_BASES:
            self.database_path = self._select_firefox_profile(
                FIREFOX_BASES[name], db_file, profile_last_updated
            )
        elif name in FIXED_PATHS:
            self.database_path = FIXED_PATHS[name]
        else:
            raise ValueError(f"Unsupported browser name: {name}")

        if not self.database_path or not self.database_path.exists():
            raise FileNotFoundError(
                f"History database not found exactly at: '{self.database_path}'"
            )

    def _select_chromium_profile(
        self, base: Path, db_file: str, last_updated: bool
    ) -> Path:
        candidates = _get_chromium_profiles(base, db_file)
        if not candidates:
            return None
        return (
            max(candidates, key=lambda p: p.stat().st_mtime)
            if last_updated
            else candidates[0]
        )

    def _select_firefox_profile(
        self, base: Path, db_file: str, last_updated: bool
    ) -> Path:
        candidates = _get_firefox_profiles(base, db_file)
        if not candidates:
            return None
        return (
            max(candidates, key=lambda p: p.stat().st_mtime)
            if last_updated
            else candidates[0]
        )

    def _cache_copy(self, source: Path, kind: str) -> str:
        """Copy a browser database to a uniquely named cache file for safe reading.

        The browser holds its own databases open and locked, so every read
        happens against a copy. The copy is reused until the original is newer.

        "Newer" includes the write-ahead log: SQLite can keep recent writes in
        `<database>-wal` and touch the main file only when it checkpoints, so a
        main file alone can be hours out of date -- or, worse, identical to the
        copy we already have, which would make the stale copy look fresh
        forever. Both are copied together.
        """
        safe_name = self.name.replace(" ", "_")
        safe_pc = computer_name().replace(" ", "_")

        # Hash the absolute path to prevent cache collisions between different custom profiles
        path_hash = hashlib.md5(str(source).encode("utf-8")).hexdigest()[:8]
        cache_name = f"bh_{safe_pc}_{safe_name}_{path_hash}_{kind}.sqlite"
        target = self.cache_dir / cache_name

        # Check if we already have a cached copy that is up to date
        try:
            if target.exists() and self._copy_is_fresh(source, target):
                return str(target)
        except OSError:
            pass  # Fallback to copying if stat fails

        last_err = None
        for attempt in range(3):
            try:
                shutil.copy(source, target)
                break
            except OSError as e:
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        else:
            raise OSError(
                f"Failed copying {kind} DB for '{self.name}' from '{source}' to '{target}': {last_err}"
            )

        self._copy_write_ahead_log(source, target)
        if not _readable(target):
            # A log copied mid-write can be unusable; the main file alone is
            # still the complete state as of its last checkpoint. Never worse
            # than not copying the log at all.
            _discard_log(target)

        # The browser writes while we copy, and a file caught mid-transaction is
        # not a database. Copying again usually lands between writes; the point
        # is that no icon or history row is silently lost to a torn read.
        for attempt in range(2):
            if _readable(target):
                break
            time.sleep(0.02 * (attempt + 1))
            shutil.copy(source, target)
            self._copy_write_ahead_log(source, target)
            if not _readable(target):
                _discard_log(target)

        return str(target)

    @staticmethod
    def _copy_is_fresh(source: Path, target: Path) -> bool:
        newest = _newest_mtime(source)
        if newest is None or newest > target.stat().st_mtime:
            return False
        # A log beside only one of the two files means the copy did not take
        # the same state: recopy rather than read half of it.
        if _log_of(source).exists() != _log_of(target).exists():
            return False
        # Timestamps can be coarse enough for two writes to share one tick, and
        # then the copy would look fresh while the log has grown behind it.
        # Sizes cannot lie about that.
        return _size(source) == _size(target) and _size(_log_of(source)) == _size(
            _log_of(target)
        )

    @staticmethod
    def _copy_write_ahead_log(source: Path, target: Path) -> None:
        source_log = _log_of(source)
        target_log = _log_of(target)
        if not source_log.exists():
            # The browser has checkpointed since the last copy: carrying the
            # old log forward would replay writes the main file already has.
            target_log.unlink(missing_ok=True)
            return
        for attempt in range(3):
            try:
                shutil.copy(source_log, target_log)
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        # No usable log: read the main file rather than nothing at all.
        target_log.unlink(missing_ok=True)

    def _copy_database(self) -> str:
        return self._cache_copy(self.database_path, "cache")

    def icons_database(self) -> Optional[Path]:
        """The profile's favicon store, if the browser keeps one."""
        name = "Favicons" if self.timestamp_type == "chromium" else "favicons.sqlite"
        candidate = Path(self.database_path).parent / name
        return candidate if candidate.exists() else None

    def copy_icons_database(self) -> Optional[str]:
        """A readable copy of the favicon store, or None when there is none."""
        source = self.icons_database()
        if source is None:
            return None
        return self._cache_copy(source, "icons")

    def history(
        self, search_term: str = "", limit: int = 100, blocked_domains: Optional[List[str]] = None
    ) -> List["HistoryItem"]:
        db_path = self._copy_database()

        last_err = None
        for attempt in range(3):
            try:
                # Open strictly in read-only mode using a URI
                connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                break
            except OSError as e:
                last_err = e
                time.sleep(0.03 * (attempt + 1))
        else:
            raise OSError(
                f"Failed to open copied history DB for '{self.name}' at '{db_path}' after retries: {last_err}"
            )

        try:
            rows = read_history(
                connection,
                self.engine,
                terms=search_term.split(),
                blocked_domains=list(blocked_domains or ()),
                limit=limit,
            )
        finally:
            connection.close()
        return [HistoryItem(self, *row) for row in rows]

    def convert_timestamp(self, raw_time):
        seconds = self.normalize_timestamp(raw_time)
        return datetime.fromtimestamp(seconds)

    def normalize_timestamp(self, raw_time) -> float:
        if raw_time is None:
            return 0.0
        try:
            if self.timestamp_type == "chromium":
                seconds = raw_time / 1_000_000 - CHROMIUM_EPOCH_OFFSET
            elif self.timestamp_type == "unix_us":
                seconds = raw_time / 1_000_000
            else:
                seconds = 0.0
            return max(0.0, float(seconds))
        except OSError:
            return 0.0


class HistoryItem:
    """Single history row wrapper."""

    def __init__(
        self, browser: Browser, url: str, title: str, last_visit_time: int
    ) -> None:
        self.browser = browser
        self.url = url
        self.title = title.strip() if title else url
        self.last_visit_time = last_visit_time
        # Every consumer sorts or compares by this value; computing it once keeps
        # the sort key honest and cheap.
        self._normalized_time = browser.normalize_timestamp(last_visit_time)

    def timestamp(self) -> datetime:
        return self.browser.convert_timestamp(self.last_visit_time)

    def normalized_time(self) -> float:
        return self._normalized_time


def get(
    browser_name: str,
    cache_dir: Path,
    custom_profile_path: Optional[str] = None,
    profile_last_updated: bool = False,
) -> Optional[Browser]:
    browser_name = browser_name.lower()
    profile_last_updated = bool(profile_last_updated)

    if browser_name == "custom profile":
        if not custom_profile_path:
            raise ValueError("Custom profile path not provided in settings.")

        raw_path = os.path.expandvars(custom_profile_path.strip("\"' "))
        path = Path(raw_path)

        if not path.exists() or not path.is_dir():
            raise ValueError(
                f"Custom directory does not exist or is not a folder: {path}"
            )

        # Strictly check exact folder provided by the user
        if (path / "History").exists():
            return Browser(
                "custom profile",
                cache_dir,
                "chromium",
                custom_path=path,
                db_file="History",
            )
        elif (path / "places.sqlite").exists():
            return Browser(
                "custom profile",
                cache_dir,
                "unix_us",
                custom_path=path,
                db_file="places.sqlite",
            )
        else:
            raise ValueError(
                f"Neither 'History' nor 'places.sqlite' exists exactly in: '{path}'"
            )

    try:
        if browser_name in CHROMIUM_PROFILE_BASES or browser_name in FIXED_PATHS:
            return Browser(
                browser_name,
                cache_dir,
                "chromium",
                profile_last_updated=profile_last_updated,
            )
        if browser_name in FIREFOX_BASES:
            return Browser(
                browser_name,
                cache_dir,
                "unix_us",
                db_file="places.sqlite",
                profile_last_updated=profile_last_updated,
            )
        raise ValueError(f"Unsupported browser: {browser_name}")
    except FileNotFoundError:
        return None


def get_all_profiles(
    browser_name: str, cache_dir: Path, custom_profile_path: Optional[str] = None
) -> List[Browser]:
    browser_name = browser_name.lower()
    browsers_list = []

    if browser_name == "custom profile":
        if not custom_profile_path:
            return []

        raw_path = os.path.expandvars(custom_profile_path.strip("\"' "))
        path = Path(raw_path)

        if not path.exists() or not path.is_dir():
            raise ValueError(
                f"Custom directory does not exist or is not a folder: {path}"
            )

        # Strictly check exact folder provided by the user
        if (path / "History").exists():
            browsers_list.append(
                Browser(
                    "custom_profile_0",
                    cache_dir,
                    "chromium",
                    custom_path=path,
                    db_file="History",
                )
            )
        elif (path / "places.sqlite").exists():
            browsers_list.append(
                Browser(
                    "custom_profile_0",
                    cache_dir,
                    "unix_us",
                    custom_path=path,
                    db_file="places.sqlite",
                )
            )
        else:
            raise ValueError(
                f"Neither 'History' nor 'places.sqlite' exists exactly in: '{path}'"
            )

        return browsers_list

    if browser_name in CHROMIUM_PROFILE_BASES:
        base = CHROMIUM_PROFILE_BASES[browser_name]
        db_file = "History"
        ts_type = "chromium"
    elif browser_name in FIREFOX_BASES:
        base = FIREFOX_BASES[browser_name]
        db_file = "places.sqlite"
        ts_type = "unix_us"
    else:
        single_browser = get(browser_name, cache_dir)
        return [single_browser] if single_browser else []

    if not base.exists():
        return []

    if browser_name in CHROMIUM_PROFILE_BASES:
        candidates = _get_chromium_profiles(base, db_file)
    else:
        candidates = _get_firefox_profiles(base, db_file)

    for idx, path in enumerate(candidates):
        unique_name = f"{browser_name}_profile_{idx}"
        try:
            browsers_list.append(
                Browser(
                    unique_name,
                    cache_dir,
                    ts_type,
                    custom_path=path.parent,
                    db_file=db_file,
                )
            )
        except FileNotFoundError:
            continue

    return browsers_list
