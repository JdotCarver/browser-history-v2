# tools/installed_probe.py
"""Run the installed build with the owner's own settings."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "perf"))
from measure_keystroke import rpc_request  # noqa: E402

launcher = Path(os.getenv("APPDATA", "")) / "FlowLauncher"
settings_file = launcher / "Settings" / "Plugins" / "Browser History V2" / "Settings.json"
settings = json.loads(settings_file.read_text(encoding="utf-8-sig"))

installed = None
for manifest in sorted(launcher.rglob("plugin.json")):
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        continue
    if "Browser History" in data.get("Name", ""):
        installed = manifest.parent
print("installed:", installed)

environment = dict(os.environ)
if (installed / "lib").is_dir():
    environment["PYTHONPATH"] = str(installed / "lib")

for query in ("wide", "auto"):
    run = subprocess.run(
        [sys.executable, "run.py", rpc_request(query, settings)],
        cwd=installed, env=environment, capture_output=True, text=True, timeout=180,
    )
    try:
        rows = json.loads(run.stdout)["result"]
    except (ValueError, KeyError):
        print(f"{query!r}: no JSON:", run.stdout[:200], run.stderr[:300])
        continue
    print(f"\nquery {query!r} with the owner's settings: {len(rows)} rows")
    for row in rows:
        icon = row.get("IcoPath") or ""
        title = row.get("Title", "").encode("ascii", "replace").decode("ascii")[:32]
        sub = row.get("SubTitle", "").encode("ascii", "replace").decode("ascii")[:38]
        state = "NO ICON" if not icon else ("missing file" if not Path(icon).exists() else Path(icon).name)
        print(f"  {title:34} {sub:40} {state}")
