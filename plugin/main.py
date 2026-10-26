from flox import Flox, ICON_HISTORY, ICON_BROWSER, ICON_FILE
import browsers
from formatting import (
    DATE_ABSOLUTE,
    LAYOUT_BRACKETED,
    TIME_12,
    URL_FULL,
    format_subtitle,
)
from favicons import FaviconStore
from results import newest_unique, read_profiles
import platform
import shutil
import time
from pathlib import Path
from datetime import datetime

# Constants
HISTORY_GLYPH = ""
CUSTOM_PROFILE = "custom profile"


class BrowserHistory(Flox):

    # Display defaults live on the class so that an instance built without
    # __init__ (the tests do this) still renders exactly like a configured one.
    show_favicons = False
    _ready = False
    date_display = DATE_ABSOLUTE
    time_format = TIME_12
    url_display = URL_FULL
    subtitle_layout = LAYOUT_BRACKETED

    def __init__(self):
        super().__init__()

        # Get the exact Flow Launcher Settings/Plugins/BrowserHistory folder
        self.cache_dir = Path(self.settings_path).parent / "Cache"


        # Read the unified dropdown
        self.default_browser = self.settings.get(
            "default_browser", "All Browsers"
        ).lower()
        self.all_browsers_history = self.default_browser == "all browsers"

        custom_profile_path = self.settings.get("custom_profile_path", "")
        self.custom_paths = [
            p.strip() for p in custom_profile_path.split(",") if p.strip()
        ]
        self.history_limit = int(self.settings.get("history_limit", 1000))
        self.max_ui_results = int(self.settings.get("max_ui_results", 500))

        # Determine profile handling from the new dropdown setting
        profile_mode = self.settings.get("profile_search_mode", "Active Profile")
        self.search_all_profiles = profile_mode == "All Profiles"
        self.profile_last_updated = profile_mode == "Active Profile"

        # How results are written. The defaults are the original look, so an
        # existing settings file (which has none of these keys) is unaffected.
        self.date_display = self.settings.get("date_display", DATE_ABSOLUTE)
        self.time_format = self.settings.get("time_format", TIME_12)
        self.url_display = self.settings.get("url_display", URL_FULL)
        self.subtitle_layout = self.settings.get("subtitle_layout", LAYOUT_BRACKETED)
        # Flow Launcher hands checkboxes back as booleans or as strings,
        # depending on how the settings were written.
        self.show_favicons = str(self.settings.get("show_favicons", False)).strip().lower() in (
            "true",
            "1",
            "yes",
        )

        blocked_domains_str = self.settings.get("blocked_domains") or ""
        self.blocked_domains = [
            domain.strip().lower()
            for domain in blocked_domains_str.split(",")
            if domain.strip()
        ]
        self.init_error = None

        # Browsers are found on the first query, not here: a context-menu
        # request never reads history, and exploring every browser's folders
        # is the single most expensive thing this plugin can do up front.
        self.browsers = []
        self._ready = False
        self.favicons = FaviconStore(self.cache_dir, [])


    def _ensure_ready(self):
        """Everything a query needs and a context menu does not.

        Flow Launcher runs this plugin as a fresh process for every keystroke
        and every context menu, and the library it builds on does the work only
        at interpreter shutdown -- so anything done eagerly in `__init__` is
        paid on the menu path too, where no history is read at all.
        """
        if self._ready:
            return
        self._ready = True

        current_pc = platform.node()
        saved_pc = self.settings.get("last_computer_name", "")
        keep_cache = self.settings.get("keep_history_cache", False)

        # If the computer name changed (e.g., Portable mode moved to a new PC), wipe the cache
        if current_pc != saved_pc:
            if not keep_cache:
                if self.cache_dir.exists():
                    shutil.rmtree(self.cache_dir, ignore_errors=True)
            self.settings["last_computer_name"] = current_pc

        # Ensure the cache directory exists for this session
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Garbage collection for old caches
        current_time = time.time()
        last_cleanup = self.settings.get("last_cache_cleanup", 0)
        if current_time - last_cleanup > 86400:  # Check once every 24 hours
            for cache_file in self.cache_dir.iterdir():
                if cache_file.is_file():
                    if current_time - cache_file.stat().st_mtime > (7 * 86400):
                        try:
                            cache_file.unlink()
                        except OSError:
                            pass
            self.settings["last_cache_cleanup"] = current_time

        self.cache_dir.mkdir(parents=True, exist_ok=True)


        if self.all_browsers_history:
            browser_names = (
                list(browsers.CHROMIUM_PROFILE_BASES.keys())
                + list(browsers.FIREFOX_BASES.keys())
                + list(browsers.FIXED_PATHS.keys())
            )
        else:
            browser_names = [self.default_browser]

        for browser_name in browser_names:
            # Handle the manual custom profile choice robustly
            if browser_name == CUSTOM_PROFILE:
                if not self.custom_paths:
                    self.init_error = "Custom Profile selected, but no directory paths were provided in settings."
                    continue
                for c_path in self.custom_paths:
                    try:
                        if self.search_all_profiles:
                            profiles = browsers.get_all_profiles(
                                browser_name,
                                self.cache_dir,
                                custom_profile_path=c_path,
                            )
                            if profiles:
                                self.browsers.extend(profiles)
                        else:
                            b = browsers.get(
                                browser_name,
                                self.cache_dir,
                                custom_profile_path=c_path,
                                profile_last_updated=self.profile_last_updated,
                            )
                            if b:
                                self.browsers.append(b)
                    except Exception as e:
                        self.init_error = str(e)  # Bubbles the exact error up to the UI
                continue

            # Auto-detect standard profiles or search all
            if self.search_all_profiles:
                self.browsers.extend(
                    browsers.get_all_profiles(browser_name, self.cache_dir)
                )
            else:
                b = browsers.get(
                    browser_name,
                    self.cache_dir,
                    profile_last_updated=self.profile_last_updated,
                )
                if b:
                    self.browsers.append(b)

        # In "All Browsers" mode, if a custom path is ALSO provided, append it
        if self.all_browsers_history and self.custom_paths:
            for c_path in self.custom_paths:
                try:
                    if self.search_all_profiles:
                        b_custom_list = browsers.get_all_profiles(
                            CUSTOM_PROFILE,
                            self.cache_dir,
                            custom_profile_path=c_path,
                        )
                        self.browsers.extend(b_custom_list)
                    else:
                        b_custom = browsers.get(
                            CUSTOM_PROFILE,
                            self.cache_dir,
                            custom_profile_path=c_path,
                            profile_last_updated=self.profile_last_updated,
                        )
                        if b_custom:
                            self.browsers.append(b_custom)
                except Exception:
                    pass  # Fail silently for the background custom injection so that don't block normal browsers

        # Different browser variants can resolve to the same profile database path.
        # Keep only the first instance per resolved DB path to avoid duplicate reads/caches.
        unique_browsers = []
        seen_db_paths = set()
        for browser in self.browsers:
            try:
                db_key = str(browser.database_path.resolve()).lower()
            except Exception:
                db_key = str(browser.database_path).lower()

            if db_key in seen_db_paths:
                continue

            seen_db_paths.add(db_key)
            unique_browsers.append(browser)

        self.browsers = unique_browsers

        self.browsers = unique_browsers
        if not self.browsers and not self.init_error:
            self.init_error = "Could not locate any supported browser profile databases."
        self.favicons.browsers = self.browsers

    def query(self, query):
        self._ensure_ready()

        if not self.browsers:
            self.add_item(
                title="Browser not available",
                subtitle=self.init_error or "Unknown error initializing browser.",
            )
            return

        try:
            source_items = self._get_combined_history(query)
            # One clock reading for the whole list: two rows a second apart
            # must not disagree about whether it is still "today".
            now = datetime.now()

            # Icons are looked up after the rows are known, so the icon
            # database is only opened when icons are switched on.
            icon_paths = {}
            if self.show_favicons:
                try:
                    icon_paths = self.favicons.resolve(source_items)
                except Exception:
                    icon_paths = {}  # never lose results over an icon

            if getattr(self, "_warnings", None):
                self.add_item(
                    title="Some browsers or profiles were skipped",
                    subtitle="; ".join(self._warnings)[:150],
                    icon=ICON_BROWSER,
                )

            for idx, item in enumerate(source_items):
                icon = icon_paths.get(item.url)
                self.add_item(
                    title=item.title,
                    subtitle=format_subtitle(
                        item.timestamp(),
                        item.url,
                        now=now,
                        date_display=self.date_display,
                        time_format=self.time_format,
                        url_display=self.url_display,
                        subtitle_layout=self.subtitle_layout,
                    ),
                    # A glyph and an icon path would fight for the same slot.
                    icon=icon or ICON_HISTORY,
                    glyph=None if icon else HISTORY_GLYPH,
                    method=self.browser_open,
                    parameters=[item.url],
                    context=[item.title, item.url],
                )
        except Exception as e:
            self.add_item(
                title="An error occurred",
                subtitle=str(e),
            )

    def _get_combined_history(self, query):
        # No profile can contribute more rows than the UI will ever show, and a
        # single profile's rows are already newest-first, so reading deeper than
        # that only moves rows that would be discarded anyway.
        row_budget = min(self.history_limit, self.max_ui_results)

        items, self._warnings = read_profiles(
            self.browsers, query, row_budget, self.blocked_domains
        )
        return newest_unique(items, self.max_ui_results)

    def context_menu(self, data):
        self.add_item(
            title="Open in browser",
            subtitle=data[0],
            icon=ICON_BROWSER,
            method=self.browser_open,
            parameters=[data[1]],
        )
        self.add_item(
            title="Copy to clipboard",
            subtitle=data[1],
            icon=ICON_FILE,
            method=self.copy_to_clipboard,
            parameters=[data[1]],
        )

    def copy_to_clipboard(self, data):
        # Imported here: the module loads a Windows library, and the context
        # menu is built on the path where nothing should be loading anything.
        import pyperclip

        pyperclip.copy(data)
        self.show_msg("Copied!", f"{data}")

    def run(self):
        pass


if __name__ == "__main__":
    BrowserHistory().run()
