# plugin/preview.py
"""The details panel that Flow Launcher shows beside the results.

Flow renders a result's `Preview` - a `PreviewInfo` record it deserialises from
the same JSON-RPC payload as everything else - so a Python plugin can populate
the panel without any C#.

What it accepts depends on the Flow version, and the difference matters:

    Flow 2.1.4 (released)   PreviewImagePath, IsMedia, Description  (plain text)
    Flow dev                ... plus ContentType ("markdown")

The panel is plain text on a released Flow, so this writes plain text: markdown
syntax would be shown literally. `Description` is drawn at the foot of the panel
and, left unset, falls back to `SubTitle` - which is why the panel otherwise
repeats the row.

So this puts the things a one-line row cannot: the whole address, the exact
time, and which browser knows the page. All of it is text the plugin already
has, so it costs nothing, and there is no table of sites to keep up to date.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from formatting import TIME_12, format_date

#: The plugin's own naming: `brave_profile_0` is "Brave" to a reader. Only that
#: form is rewritten -- a browser genuinely called "Custom Profile 2" keeps it.
_INTERNAL_PROFILE_NAME = re.compile(r"^(?P<base>.+?)_profile_\d+$", re.IGNORECASE)


def browser_label(item) -> str:
    """The browser a row came from, in the words its own UI uses."""
    browser = getattr(item, "browser", None)
    name = str(getattr(browser, "name", "") or "")
    if not name:
        return ""
    internal = _INTERNAL_PROFILE_NAME.match(name)
    if internal:
        name = internal.group("base")
    return name.replace("_", " ").replace("  ", " ").strip().title()


def build_preview(
    item,
    now: datetime,
    time_format: str = TIME_12,
    icon: Optional[str] = None,
) -> dict:
    """The details panel payload for one result."""
    moment = item.timestamp()
    relative = format_date(moment, now, relative=True, time_format=time_format)
    absolute = format_date(moment, now, relative=False, time_format=time_format)

    # For an older visit the relative form is the date alone, which the absolute
    # form already contains: say it once, with the time.
    if relative == absolute or absolute.startswith(relative):
        visited = absolute
    else:
        visited = f"{relative} - {absolute}"
    lines = [item.url, "", f"Visited: {visited}"]

    browser = browser_label(item)
    if browser:
        lines.append(f"Browser: {browser}")

    preview = {"Description": "\n".join(lines)}
    if icon:
        # Flow falls back to the result icon, so this only matters when icons
        # are switched on and one was actually found.
        preview["PreviewImagePath"] = icon
    return preview
