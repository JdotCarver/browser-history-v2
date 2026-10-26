# tools/perf/measure_keystroke.py
"""Time one keystroke end to end: interpreter, imports, init, query, render.

Flow Launcher starts a fresh `python run.py <json>` for every keystroke, so the
perceived lag is the whole process, not just the database work. This measures
that process the way the launcher does it:

1. a portable-mode Flow layout in a temporary directory
   (`<tmp>/UserData/FlowLauncher/Plugins/browser-history-v2`, which is what makes
   the launcher library resolve its own paths without a real installation),
2. the plugin's real dependencies installed into `lib/`, exactly as Flow ships
   them,
3. `run.py` invoked with a real RPC request payload, timed as a subprocess.

Then it splits the number: interpreter, interpreter + launcher library,
+ plugin imports, + the rest (settings, profile discovery, reading, rendering).

The measurements use whatever browsers are installed for the *current* user, so
on a real desktop the query numbers are real. No URLs, titles or search terms are
printed; only timings and result counts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diagnose import pick_search_term  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "browser-history-v2"
MANIFEST = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))

#: Mirrors SettingsTemplate.yaml's defaults, so the measurement reflects a plugin
#: nobody has configured yet.
DEFAULT_SETTINGS = {
    "default_browser": "All Browsers",
    "profile_search_mode": "All Profiles",
    "custom_profile_path": "",
    "history_limit": "1000",
    "max_ui_results": "500",
    "blocked_domains": "",
    "keep_history_cache": False,
}

#: Long enough that nothing real matches it.
ABSENT_TERM = "qzxqzxqzx"


def build_plugin_layout(work_dir: Path) -> tuple[Path, Path]:
    """Create a portable-mode Flow tree containing a copy of this plugin."""
    launcher_root = work_dir / "UserData" / "FlowLauncher"
    plugin_dir = launcher_root / "Plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)

    shutil.copy2(REPO_ROOT / "run.py", plugin_dir / "run.py")
    shutil.copy2(REPO_ROOT / "plugin.json", plugin_dir / "plugin.json")
    shutil.copytree(REPO_ROOT / "plugin", plugin_dir / "plugin")

    settings_dir = launcher_root / "Settings" / "Plugins" / MANIFEST["Name"]
    settings_dir.mkdir(parents=True)
    (settings_dir / "Settings.json").write_text(
        json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8"
    )

    # Flow keeps one app-level settings file that maps each plugin to its action
    # keyword. The launcher library reads it while rendering an item (for the
    # autocomplete text), and fails the whole query without it.
    app_settings = launcher_root / "Settings" / "Settings.json"
    app_settings.write_text(
        json.dumps(
            {
                "PluginSettings": {
                    "Plugins": {MANIFEST["ID"]: {"UserKeywords": [MANIFEST["ActionKeyword"]]}}
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return plugin_dir, launcher_root


def install_dependencies(plugin_dir: Path) -> Path:
    """Install the plugin's requirements into `lib/`, as the packaging does."""
    lib_dir = plugin_dir / "lib"
    if lib_dir.exists() and any(lib_dir.iterdir()):
        return lib_dir

    requirements = REPO_ROOT / "requirements.txt"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--target",
            str(lib_dir),
            "--requirement",
            str(requirements),
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "Could not install the plugin's requirements into the temporary "
            f"layout:\n{completed.stderr.strip()}"
        )
    return lib_dir


def time_process(command: list[str], *, cwd: Path, environment: dict, repeats: int) -> tuple[float, str]:
    """Median wall time of a whole process, plus the output of its last run."""
    samples = []
    output = ""
    for _ in range(repeats):
        started = time.perf_counter()
        completed = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True)
        samples.append((time.perf_counter() - started) * 1000)
        output = completed.stdout
    return statistics.median(samples), output


def rpc_request(query: str) -> str:
    """The payload Flow Launcher passes as argv[1] for a query."""
    return json.dumps(
        {
            "method": "query",
            "parameters": [query],
            "settings": DEFAULT_SETTINGS,
        }
    )


def result_count(output: str) -> int:
    """How many items the plugin rendered, read back out of its JSON reply."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "result" in payload:
            return len(payload["result"])
    return -1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    work_dir = Path(tempfile.mkdtemp(prefix="bh-keystroke-"))
    report: dict = {"machine": {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cores": os.cpu_count(),
    }, "steps": {}, "keystrokes": {}}

    try:
        plugin_dir, _launcher_root = build_plugin_layout(work_dir)
        lib_dir = install_dependencies(plugin_dir)

        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)

        # 1. The floor: starting an interpreter that does nothing.
        report["steps"]["interpreter"], _ = time_process(
            [sys.executable, "-c", "pass"], cwd=plugin_dir, environment=environment, repeats=args.repeats
        )

        # 2. Plus the launcher library the plugin cannot live without.
        with_lib = dict(environment, PYTHONPATH=str(lib_dir))
        report["steps"]["interpreter+flox"], _ = time_process(
            [sys.executable, "-c", "import flox"], cwd=plugin_dir, environment=with_lib, repeats=args.repeats
        )

        # 3. Plus this plugin's own modules.
        report["steps"]["interpreter+flox+plugin"], _ = time_process(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path[:0] = [{str(plugin_dir)!r}, {str(plugin_dir / 'plugin')!r}, {str(lib_dir)!r}];"
                " import main",
            ],
            cwd=plugin_dir,
            environment=environment,
            repeats=args.repeats,
        )

        # 4. The whole keystroke, through the real entry point.
        # The search scenario uses a word that really occurs in this machine's
        # history, so the timing reflects a search that matches. The word is
        # history content: it is used, never printed.
        search_term = pick_search_term(Path(tempfile.mkdtemp(prefix="bh-term-")))
        scenarios = [
            ("idle", "", "no search term: the listing Flow shows on arrival"),
            ("search", search_term, "a word that occurs often in this history"),
            ("absent", ABSENT_TERM, "still typing: matches nothing"),
        ]

        for key, query, description in scenarios:
            if query == "" and key == "search":
                report["keystrokes"][key] = {
                    "ms": 0.0,
                    "results": -1,
                    "description": "skipped: no history to draw a search term from",
                }
                continue
            elapsed, output = time_process(
                [sys.executable, "run.py", rpc_request(query)],
                cwd=plugin_dir,
                environment=environment,
                repeats=args.repeats,
            )
            report["keystrokes"][key] = {
                "ms": round(elapsed, 1),
                "results": result_count(output),
                "description": description,
            }

        if not args.json:
            print(f"machine: {report['machine']}")
            print(f"plugin: {plugin_dir.relative_to(work_dir)}")
            print()
            print(f"{'step':<32}{'median':>10}")
            print("-" * 42)
            for label, milliseconds in report["steps"].items():
                print(f"{label:<32}{milliseconds:>8.1f} ms")
            print()
            print(f"{'keystroke':<32}{'median':>10}{'results':>10}")
            print("-" * 52)
            for key, data in report["keystrokes"].items():
                print(f"{key:<32}{data['ms']:>8.1f} ms{data['results']:>10}")
            print()
            for key, data in report["keystrokes"].items():
                print(f"  {key:<11} {data['description']}")
            remaining = {
                key: round(data["ms"] - report["steps"]["interpreter+flox+plugin"], 1)
                for key, data in report["keystrokes"].items()
            }
            report["plugin_work_only"] = remaining
            print()
            print(f"plugin work only (keystroke minus imports): {remaining}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
