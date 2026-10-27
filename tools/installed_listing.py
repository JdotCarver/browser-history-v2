# tools/installed_listing.py
"""Find the installed Browser History V2 code and describe it."""

import hashlib
import json
import os
from pathlib import Path

root = Path(os.getenv("APPDATA", "")) / "FlowLauncher"
print("looking under:", root)

for manifest in sorted(root.rglob("plugin.json")):
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        continue
    if "Browser History" not in data.get("Name", ""):
        continue
    folder = manifest.parent
    print(f"\n{folder}")
    print(f"  version: {data.get('Version')}  keyword: {data.get('ActionKeyword')}")
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.py"))
    print(f"  python files: {len(files)}")
    for name in files:
        if name.endswith(("favicons.py", "main.py", "preview.py")):
            digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()[:12]
            print(f"    {name} ({digest})")
        print(f"    . {name}") if False else None
    cache = folder / "Cache" / "favicons" / "index.json"
    print(f"  cache beside the code: {'yes' if cache.exists() else 'no'}")
