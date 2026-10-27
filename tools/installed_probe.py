# tools/installed_probe.py
"""Run the *installed* plugin against the real profile and show what Flow gets."""

import base64
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "perf"))
from measure_keystroke import DEFAULT_SETTINGS, rpc_request  # noqa: E402


def describe(path: Path) -> str:
    data = path.read_bytes()
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    return f"{width}x{height} depth={depth} colour={colour} interlace={interlace} bytes={len(data)}"


def main() -> int:
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
        return 0

    print("installed:", installed)
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
            title = row.get("Title", "").encode("ascii", "replace").decode("ascii")[:34]
            if not icon or not Path(icon).exists():
                print(f"  {title:36} icon=NO")
                continue
            path = Path(icon)
            print(f"  {title:36} icon=yes {path.name} {describe(path)}")
            print("  b64:", base64.b64encode(path.read_bytes()).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
