#!/usr/bin/env python3
"""Read the plugin's request trace and say what is actually happening.

Turn on "Trace Requests" in the plugin settings, use Flow Launcher normally for
a minute or two, then:

    python tools/perf/read_trace.py "C:/path/to/FlowLauncher/Settings/Plugins/Browser History V2/Cache/requests.jsonl"

What it answers, in order of usefulness:

  * how many processes each interaction costs -- two launches a few hundred
    milliseconds apart for one press means the request was started twice;
  * where the milliseconds go inside a request (interpreter, imports, flox,
    construction, discovery, our work);
  * the gap between successive requests, which is the observable side of any
    debounce Flow applies before it starts the plugin at all.

The trace holds numbers and RPC method names only: no queries, no URLs.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Two launches closer together than this are treated as one interaction that
# produced two processes -- the signature of a double invocation.
BURST_GAP_MS = 400

PHASE_ORDER = [
    ("creation_to_python_start", "OS process created -> Python starts"),
    ("entry_to_imports_done", "run.py start -> imports done"),
    ("imports_to_constructed", "imports -> plugin constructed"),
    ("constructed_to_rpc", "constructed -> RPC begins"),
    ("rpc_to_ready", "RPC -> discovery done"),
    ("ready_to_rendered", "discovery -> results built"),
    ("total_since_process_start", "whole process"),
]


def load(path: Path) -> List[dict]:
    records: List[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "ts" in record and "method" in record:
            records.append(record)
    records.sort(key=lambda record: float(record["ts"]))
    return records


def phase_table(records: List[dict]) -> None:
    if not records:
        return
    print(f"{'phase':<34}{'query':>10}{'menu':>10}")
    print("-" * 56)
    for key, label in PHASE_ORDER:
        cells = []
        for method in ("query", "context_menu"):
            values = [
                float(record["phases"][key])
                for record in records
                if record["method"] == method and key in record.get("phases", {})
            ]
            cells.append(f"{statistics.median(values):.1f}" if values else "-")
        print(f"{label:<34}{cells[0]:>10}{cells[1]:>10}")
    print("(medians, milliseconds)")


def bursts(records: List[dict]) -> List[dict]:
    """Groups of requests that arrived too close together to be separate actions."""
    found: List[dict] = []
    index = 0
    while index < len(records):
        start = float(records[index]["ts"])
        group = [records[index]]
        cursor = index + 1
        while cursor < len(records) and (float(records[cursor]["ts"]) - start) * 1000 < BURST_GAP_MS:
            group.append(records[cursor])
            cursor += 1
        if len(group) > 1:
            found.append(
                {
                    "start": start,
                    "methods": [record["method"] for record in group],
                    "spread_ms": round((float(group[-1]["ts"]) - start) * 1000, 1),
                }
            )
        index = cursor
    return found


def gaps(records: List[dict], method: Optional[str] = None) -> List[float]:
    selected = [record for record in records if method is None or record["method"] == method]
    if len(selected) < 2:
        return []
    return [
        round((float(second["ts"]) - float(first["ts"])) * 1000, 1)
        for first, second in zip(selected, selected[1:])
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("trace", help="path to requests.jsonl")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.trace)
    if not path.exists():
        print(f"No trace at {path}. Turn on 'Trace Requests', use Flow, then run this again.")
        return 1

    records = load(path)
    if not records:
        print("The trace is empty: no request has been recorded yet.")
        return 1

    counts: Dict[str, int] = {}
    for record in records:
        counts[record["method"]] = counts.get(record["method"], 0) + 1

    groups = bursts(records)
    all_gaps = gaps(records)
    query_gaps = gaps(records, "query")
    flows = sorted({record.get("flow", "") for record in records if record.get("flow")})

    if args.json:
        print(
            json.dumps(
                {
                    "requests": len(records),
                    "by_method": counts,
                    "phases": {
                        key: {
                            method: statistics.median(
                                [
                                    float(record["phases"][key])
                                    for record in records
                                    if record["method"] == method
                                    and key in record.get("phases", {})
                                ]
                            )
                            for method in ("query", "context_menu")
                            if any(
                                record["method"] == method and key in record.get("phases", {})
                                for record in records
                            )
                        }
                        for key, _label in PHASE_ORDER
                    },
                    "bursts": groups,
                    "gaps_all": all_gaps,
                    "gaps_query": query_gaps,
                    "flow": flows,
                },
                indent=2,
            )
        )
        return 0

    print(f"trace: {path}")
    print(f"requests: {len(records)}  ({', '.join(f'{k} {v}' for k, v in counts.items())})")
    if flows:
        print(f"flow launcher version: {', '.join(flows)}")
    print()

    phase_table(records)

    print()
    print(f"requests closer together than {BURST_GAP_MS} ms: {len(groups)}")
    for group in groups[:10]:
        print(f"  t={group['start']:.2f}  {' + '.join(group['methods'])}  (within {group['spread_ms']} ms)")
    if len(groups) > 10:
        print(f"  ... and {len(groups) - 10} more")

    if all_gaps:
        print()
        print(f"gap between requests: median {statistics.median(all_gaps):.0f} ms, "
              f"min {min(all_gaps):.0f}, max {max(all_gaps):.0f}")
    if query_gaps:
        print(f"gap between queries: median {statistics.median(query_gaps):.0f} ms "
              f"(typing cadence + any debounce Flow applies)")

    print()
    print("Reading this:")
    print("  * A burst of two 'query' entries is Flow re-running the plugin for one")
    print("    keystroke; a burst of 'query' then 'context_menu' means opening the")
    print("    menu also refreshed the list, and the user waits for both.")
    print("  * 'OS process created -> Python starts' is the interpreter boot plus the")
    print("    antivirus scan of the executable. It is not Python code and it is paid")
    print("    identically by a query and by the menu, which is why both feel alike.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
