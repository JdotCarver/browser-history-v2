# tests/test_read_trace.py
"""The trace reader's arithmetic: bursts, gaps, phase medians."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "perf"))

import read_trace  # noqa: E402


def record(ts, method="query", phases=None, **extra):
    return {"ts": ts, "method": method, "phases": phases or {"ready_to_rendered": 1.0}, **extra}


class TempTrace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "requests.jsonl"

    def write(self, records):
        self.path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


class Loading(TempTrace):
    def test_records_come_back_in_time_order(self):
        self.write([record(2.0), record(1.0)])
        self.assertEqual([r["ts"] for r in read_trace.load(self.path)], [1.0, 2.0])

    def test_damaged_lines_are_skipped(self):
        self.path.write_text('{"ts": 1, "method": "query"}\nnot json\n\n{"junk": true}\n')
        self.assertEqual(len(read_trace.load(self.path)), 1)


class Duplicates(TempTrace):
    """One action, two processes -- invisible in any per-request timing."""

    def test_two_menus_inside_a_quarter_second_are_one_press(self):
        # The owner's machine: every press produced exactly this pair.
        self.write([record(100.0, "context_menu"), record(100.121, "context_menu")])
        groups = read_trace.duplicates(read_trace.load(self.path))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["method"], "context_menu")
        self.assertEqual(groups[0]["count"], 2)
        self.assertAlmostEqual(groups[0]["spread_ms"], 121.0, places=1)

    def test_typing_is_not_a_duplicate(self):
        # 118-300 ms between keystrokes is a person typing.
        self.write([record(1.0), record(1.23), record(1.46), record(1.75)])
        self.assertEqual(read_trace.duplicates(read_trace.load(self.path)), [])

    def test_queries_closer_than_a_keystroke_are_a_duplicate(self):
        self.write([record(1.0), record(1.05)])
        groups = read_trace.duplicates(read_trace.load(self.path))
        self.assertEqual([group["method"] for group in groups], ["query"])

    def test_a_menu_after_a_query_is_not_one_action(self):
        self.write([record(1.0, "query"), record(1.02, "context_menu")])
        self.assertEqual(read_trace.duplicates(read_trace.load(self.path)), [])

    def test_separate_presses_are_not_paired(self):
        self.write([record(1.0, "context_menu"), record(1.4, "context_menu")])
        self.assertEqual(read_trace.duplicates(read_trace.load(self.path)), [])


class Gaps(TempTrace):
    def test_gaps_are_milliseconds_between_successive(self):
        self.write([record(1.0), record(1.25), record(2.0)])
        self.assertEqual(read_trace.gaps(read_trace.load(self.path)), [250.0, 750.0])

    def test_gaps_can_be_filtered_by_method(self):
        self.write([record(1.0), record(1.5, "context_menu"), record(2.0)])
        self.assertEqual(read_trace.gaps(read_trace.load(self.path), "query"), [1000.0])

    def test_one_record_has_no_gaps(self):
        self.write([record(1.0)])
        self.assertEqual(read_trace.gaps(read_trace.load(self.path)), [])


class CommandLine(unittest.TestCase):
    def test_a_missing_trace_is_explained_not_crashed(self):
        self.assertEqual(read_trace.main(["/nonexistent/requests.jsonl"]), 1)

    def test_an_empty_trace_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.jsonl"
            path.write_text("")
            self.assertEqual(read_trace.main([str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
