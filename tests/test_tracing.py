# tests/test_tracing.py
"""Request tracing: off by default, and safe when it is on."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import tracing  # noqa: E402
from tracing import RequestTrace  # noqa: E402


class TempTrace(unittest.TestCase):
    def setUp(self):
        # In a real request run.py marks the process start before importing.
        tracing.mark_process_start()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "Cache" / "requests.jsonl"

    def lines(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


class Disabled(TempTrace):
    def test_nothing_is_written(self):
        trace = RequestTrace(self.path, enabled=False)
        trace.mark("rpc")
        trace.finish("query")
        self.assertFalse(self.path.exists())

    def test_marks_are_harmless(self):
        trace = RequestTrace(self.path, enabled=False)
        trace.mark("a")
        trace.mark("b")
        self.assertEqual(trace.marks, {})


class Enabled(TempTrace):
    def test_one_line_per_request(self):
        for _ in range(2):
            trace = RequestTrace(self.path, enabled=True)
            trace.mark("rpc")
            trace.finish("query")
        self.assertEqual(len(self.lines()), 2)

    def test_the_record_says_what_happened(self):
        trace = RequestTrace(self.path, enabled=True)
        trace.mark("imported")
        trace.mark("constructed")
        trace.mark("rpc")
        trace.mark("ready")
        trace.mark("rendered")
        trace.finish("context_menu", flow="1.20.1", results=2)

        record = self.lines()[0]
        self.assertEqual(record["method"], "context_menu")
        self.assertEqual(record["flow"], "1.20.1")
        self.assertEqual(record["results"], 2)
        self.assertEqual(record["pid"], os.getpid())
        for key in ("constructed_to_rpc", "rpc_to_ready", "ready_to_rendered"):
            self.assertIn(key, record["phases"])
            self.assertGreaterEqual(record["phases"][key], 0)

    def test_no_search_text_is_recorded(self):
        """A trace is numbers and a method name: nothing a user typed."""
        trace = RequestTrace(self.path, enabled=True)
        trace.mark("rpc")
        trace.finish("query", results=3)
        text = self.path.read_text().lower()
        for leak in ("http", "title", "query_text", "url\""):
            self.assertNotIn(leak, text)

    def test_an_empty_directory_is_created(self):
        trace = RequestTrace(self.path, enabled=True)
        trace.finish("query")
        self.assertTrue(self.path.parent.is_dir())

    def test_a_wrong_method_timestamp_is_simply_missing(self):
        trace = RequestTrace(self.path, enabled=True)
        trace.finish("query")
        record = self.lines()[0]
        self.assertNotIn("rpc_to_ready", record["phases"])
        self.assertIn("total_since_process_start", record["phases"])

    def test_the_file_stays_bounded(self):
        original_max, original_trim = tracing.MAX_LINES, tracing.TRIM_TO
        tracing.MAX_LINES, tracing.TRIM_TO = 6, 3
        try:
            for _ in range(10):
                RequestTrace(self.path, enabled=True).finish("query")
            self.assertLessEqual(len(self.lines()), tracing.MAX_LINES)
        finally:
            tracing.MAX_LINES, tracing.TRIM_TO = original_max, original_trim

    def test_a_broken_path_never_raises(self):
        trace = RequestTrace(Path("/proc/definitely/not/writable.jsonl"), enabled=True)
        trace.mark("rpc")
        trace.finish("query")  # must not raise


class CreationTime(TempTrace):
    """The gap before Python exists is the one no Python code can time itself."""

    def test_a_known_creation_time_becomes_a_phase(self):
        import os as _os
        _os.environ["BH_PROCESS_CREATED"] = repr(float(_os.environ["BH_PROCESS_EPOCH"]) - 0.040)

        trace = RequestTrace(self.path, enabled=True)
        trace.finish("query")

        phases = self.lines()[0]["phases"]
        self.assertGreaterEqual(phases["creation_to_python_start"], 30)
        self.assertLess(phases["creation_to_python_start"], 5000)

    def test_without_a_creation_time_the_phase_is_simply_absent(self):
        import os as _os
        _os.environ.pop("BH_PROCESS_CREATED", None)

        trace = RequestTrace(self.path, enabled=True)
        trace.finish("query")

        self.assertNotIn("creation_to_python_start", self.lines()[0]["phases"])


class ProcessStart(unittest.TestCase):
    def test_the_entry_point_records_when_the_process_started(self):
        tracing.mark_process_start()
        self.assertIsNotNone(tracing.process_start_mono())
        self.assertGreater(tracing.process_start_mono(), 0)


if __name__ == "__main__":
    unittest.main()
