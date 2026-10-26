# tests/flow_stub.py
"""Minimal stand-ins for the Flow Launcher runtime, installed into sys.modules.

`plugin/main.py` is a Flow Launcher plugin: it imports `flox` (the launcher
library, only installed inside a plugin's `lib` folder) and `pyperclip`. Both
are replaced here with just enough surface to import the module and drive it
without a launcher or a clipboard.
"""

import sys
import types

ICON_HISTORY = "history.png"
ICON_BROWSER = "browser.png"
ICON_FILE = "file.png"


class Flox:
    """The launcher base class, reduced to what `main.BrowserHistory` touches."""

    def __init__(self, *args, **kwargs):
        self.settings_path = ""
        self.settings = {}
        self.items = []

    def add_item(self, title, subtitle="", icon=None, method=None, parameters=None,
                 context=None, glyph=None, **kwargs):
        entry = {
            "title": title,
            "subtitle": subtitle,
            "icon": icon,
            "method": method,
            "parameters": parameters,
            "context": context,
            "glyph": glyph,
        }
        # The real library copies any extra keyword straight onto the item, which
        # is how Preview and PreviewVisibility reach Flow.
        for key, value in kwargs.items():
            entry[key] = value
        self.items.append(entry)

    def browser_open(self, url):
        """Provided by the real Flox base class."""
        return url

    def show_msg(self, title, message):
        return (title, message)


def install():
    """Register the stand-ins; safe to call more than once."""
    if "flox" not in sys.modules:
        flox = types.ModuleType("flox")
        flox.Flox = Flox
        flox.ICON_HISTORY = ICON_HISTORY
        flox.ICON_BROWSER = ICON_BROWSER
        flox.ICON_FILE = ICON_FILE
        sys.modules["flox"] = flox

    if "pyperclip" not in sys.modules:
        pyperclip = types.ModuleType("pyperclip")
        pyperclip.copied = []

        def copy(text):
            pyperclip.copied.append(text)

        pyperclip.copy = copy
        sys.modules["pyperclip"] = pyperclip

    return sys.modules["flox"], sys.modules["pyperclip"]
