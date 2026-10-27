# tools/installed_probe.py
"""Run the *installed* plugin against the real profile and show what Flow gets."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "perf"))
from measure_keystroke import DEFAULT_SETTINGS, rpc_request  # noqa: E402

root = Path(os.getenv("APPDATA", "")) / "FlowLauncher"
installed = None
for manifest in sorted(root.rglob("plugin.json")):
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        continue
    if "Browser History" in data.get("Name", ""):
        installed = manifest.parent
if installed is None:
    print("no installed Browser History plugin found")
    raise SystemExit(0)

print("installed:", installed, "version", data.get("Version"))
settings = dict(DEFAULT_SETTINGS, max_ui_results="25", show_favicons="True",
                url_display="Full URL")

environment = dict(os.environ)
library = installed / "lib"
if library.is_dir():
    environment["PYTHONPATH"] = str(library)

for query in ("wide", "auto"):
    run = subprocess.run(
        [sys.executable, "run.py", rpc_request(query, settings)],
        cwd=installed, env=environment, capture_output=True, text=True, timeout=120,
    )
    try:
        rows = json.loads(run.stdout)["result"]
    except (ValueError, KeyError):
        print(f"\nquery {query!r}: no JSON", run.stdout[:300], run.stderr[:300])
        continue
    print(f"\nquery {query!r}: {len(rows)} rows")
    for row in rows:
        icon = row.get("IcoPath") or ""
        exists = Path(icon).exists() if icon else False
        print(f"  {row.get('Title','')[:34]:36} icon={'yes' if exists else 'NO '} {Path(icon).name if icon else ''}")
