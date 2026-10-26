#!/usr/bin/env python3
"""Fire a few traced requests through the real entry point, then read the trace.

This is the synthetic version of what the owner should do by hand: it builds a
portable Flow tree, turns tracing on, sends the requests Flow would send, and
prints the trace summary. It exists to prove the instrumentation works on the
machine it will be used on, not to replace using the launcher.

    python tools/perf/trace_session.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from measure_keystroke import (  # noqa: E402
    build_plugin_layout,
    install_dependencies,
    rpc_request,
    settings_for,
)

PLUGIN_NAME = "Browser History V2"


def merge_reported_settings(settings: dict, output: str) -> dict:
    """What Flow does with the settings the plugin reports back.

    The launcher persists `SettingsChange`, and the plugin relies on that: the
    machine name and the cache-cleanup time only exist because the next request
    reads them back. A harness that ignores it is not modelling Flow.
    """
    try:
        payload = json.loads(output)
    except ValueError:
        return settings
    change = payload.get("SettingsChange")
    if isinstance(change, dict):
        merged = dict(settings)
        merged.update(change)
        return merged
    return settings


def fire(plugin_dir: Path, environment: dict, settings: dict, method: str, parameters):
    completed = subprocess.run(
        [sys.executable, "run.py", rpc_request(parameters, settings, method=method)],
        cwd=plugin_dir,
        env=environment,
        capture_output=True,
        text=True,
    )
    return merge_reported_settings(settings, completed.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-results", type=int, default=25)
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="bh-trace-session-"))
    settings = dict(settings_for(args.max_results, False), trace_requests=True)
    plugin_dir, launcher_root = build_plugin_layout(work, settings)
    lib_dir = install_dependencies(plugin_dir)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONPATH"] = str(lib_dir)

    trace_path = launcher_root / "Settings" / "Plugins" / PLUGIN_NAME / "requests.jsonl"

    # Two keystrokes' worth of listings, then the menu on a result.
    settings = fire(plugin_dir, environment, settings, "query", "")
    settings = fire(plugin_dir, environment, settings, "query", "")
    settings = fire(
        plugin_dir, environment, settings, "context_menu", ["A title", "https://example.com/x"]
    )

    if not trace_path.exists():
        print(f"No trace was written to {trace_path}.", file=sys.stderr)
        return 1

    completed = subprocess.run(
        [sys.executable, str(TOOLS / "read_trace.py"), str(trace_path)],
        capture_output=True,
        text=True,
    )
    print(completed.stdout or completed.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
