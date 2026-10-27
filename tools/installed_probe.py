# tools/installed_probe.py
"""What the installed plugin is configured to do, and what it returns."""

import json
import os
from pathlib import Path

root = Path(os.getenv("APPDATA", "")) / "FlowLauncher" / "Settings" / "Plugins"
settings_file = root / "Browser History V2" / "Settings.json"
data = json.loads(settings_file.read_text(encoding="utf-8-sig"))
keys = (
    "default_browser",
    "profile_search_mode",
    "show_favicons",
    "max_ui_results",
    "history_limit",
    "url_display",
    "date_display",
    "keep_history_cache",
)
print("installed settings (non-personal keys only):")
for key in keys:
    print(f"  {key}: {data.get(key)!r}")
print("  custom_profile_path set:", bool(str(data.get("custom_profile_path", "")).strip()))

cache = settings_file.parent / "Cache"
print("\ncache:", "present" if cache.exists() else "absent")
index = cache / "favicons" / "index.json"
if index.exists():
    entries = json.loads(index.read_text(encoding="utf-8"))
    print("  cached icons:", len(entries))
    for fragment in ("autocompressor", "wide.video"):
        hits = [url for url in entries if fragment in url]
        print(f"  {fragment}: {len(hits)} cached")
        for url in hits[:3]:
            print("   ", url[:90])
    files = list((cache / "favicons").glob("*.png"))
    print("  icon files:", len(files))
