# plugin/formatting.py
"""How a history row is written for the result list.

Four independent settings decide what a subtitle looks like:

    date_display     Absolute (default) | Relative
    time_format      12-hour (default) | 24-hour | System
    url_display      Full URL (default) | Domain only
    subtitle_layout  Bracketed (default) | Dashed

Every default here reproduces the plugin's original output exactly, so a user
who never opens the settings sees no change at all.

The functions take `now` explicitly rather than reading the clock, which keeps
the relative-date rules testable at any hour of the day.

Nothing in this module imports Flow Launcher: it is string handling, and the
tests exercise it directly.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

# Setting values, spelled exactly as they appear in SettingsTemplate.yaml.
DATE_ABSOLUTE = "Absolute"
DATE_RELATIVE = "Relative"

TIME_12 = "12-hour"
TIME_24 = "24-hour"
TIME_SYSTEM = "System"

URL_FULL = "Full URL"
URL_DOMAIN = "Domain only"

LAYOUT_BRACKETED = "Bracketed"
LAYOUT_DASHED = "Dashed"

# Only these schemes name a website. urlsplit reads 'chrome://settings/' as a
# host called 'settings', which is not something worth showing a reader.
WEB_SCHEMES = frozenset({"http", "https", "ftp"})


def _system_prefers_24h() -> bool:
    """Whether this machine's locale writes times without AM/PM.

    A 24-hour locale leaves `%p` empty, which is a cheaper and far more
    reliable answer than trying to guess a format string per locale.
    """
    try:
        return not time.strftime("%p").strip()
    except (ValueError, OSError):
        return False


def format_clock(moment: datetime, time_format: str) -> str:
    """The time of day alone: '07:43 PM', '19:43'."""
    if time_format == TIME_24 or (time_format == TIME_SYSTEM and _system_prefers_24h()):
        return moment.strftime("%H:%M")
    # %I pads to two digits, which is what the plugin has always shown.
    return moment.strftime("%I:%M %p")


def _absolute(moment: datetime, time_format: str) -> str:
    return f"{moment.strftime('%b %d, %Y')} {format_clock(moment, time_format)}"


def format_date(
    moment: datetime,
    now: datetime,
    relative: bool,
    time_format: str = TIME_12,
) -> str:
    """A visit's date, either as a fixed stamp or as age the reader recognises."""
    if not relative:
        return _absolute(moment, time_format)

    elapsed = (now - moment).total_seconds()
    if elapsed < -60:
        # A visit dated in the future (a clock that moved backwards, or a
        # timestamp from another machine). 'In 2 hours' would be nonsense.
        return _absolute(moment, time_format)

    if elapsed < 60:
        return "Just now"
    if elapsed < 3600:
        minutes = int(elapsed // 60)
        return f"{minutes} min ago"

    days = (now.date() - moment.date()).days
    if days <= 0:
        return f"Today at {format_clock(moment, time_format)}"
    if days == 1:
        return f"Yesterday at {format_clock(moment, time_format)}"
    if days < 7:
        # Within the week, the weekday is easier to place than a date.
        return f"{moment.strftime('%A')} at {format_clock(moment, time_format)}"
    if moment.year == now.year:
        # Older than a week, but the year is obvious from context.
        return moment.strftime("%b %d")
    return moment.strftime("%b %d, %Y")


def display_url(url: str, url_display: str = URL_FULL) -> str:
    """The URL, or just the site it points at."""
    if url_display != URL_DOMAIN:
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.netloc or parts.scheme not in WEB_SCHEMES:
        # about:, chrome://, file: -- the 'host' there is not a site name.
        return url
    netloc = parts.netloc

    site = netloc.rsplit("@", 1)[-1].lower()  # drop any user:password@
    if site.startswith("www.") and site.count(".") >= 2:
        site = site[4:]
    return site


def format_subtitle(
    moment: datetime,
    url: str,
    now: Optional[datetime] = None,
    date_display: str = DATE_ABSOLUTE,
    time_format: str = TIME_12,
    url_display: str = URL_FULL,
    subtitle_layout: str = LAYOUT_BRACKETED,
) -> str:
    """The full subtitle line for one result."""
    stamp = format_date(
        moment,
        now or datetime.now(),
        relative=date_display == DATE_RELATIVE,
        time_format=time_format,
    )
    target = display_url(url, url_display)
    if subtitle_layout == LAYOUT_DASHED:
        return f"{stamp} - {target}"
    return f"[{stamp}] {target}"
