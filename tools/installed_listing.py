from pathlib import Path
root = Path(__import__("os").getenv("APPDATA", "")) / "FlowLauncher/Settings/Plugins"
for d in sorted(root.iterdir()):
    if not d.is_dir():
        continue
    files = [p.name for p in d.iterdir()]
    print(d.name, "->", sorted(files)[:12])
    pj = d / "plugin.json"
    if pj.exists():
        print("   version:", __import__("json").loads(pj.read_text(encoding="utf-8-sig")).get("Version"))
