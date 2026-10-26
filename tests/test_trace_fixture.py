# tests/test_trace_fixture.py
"""A real trace from the owner's machine, kept because it proves a point.

Flow 2.1.4 on Windows 10: 52 requests over about a minute of ordinary use, no
search text or URLs in it -- only method names, PIDs, timings and the Flow
version. It is here because it is the only evidence of its kind we have.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools" / "perf"))

import read_trace  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "trace-owner-windows-flow-2.1.4.jsonl"


class OwnerTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = read_trace.load(FIXTURE)
        cls.duplicates = read_trace.duplicates(cls.records)

    def test_the_fixture_is_intact(self):
        self.assertEqual(len(self.records), 52)
        methods = {record["method"] for record in self.records}
        self.assertEqual(methods, {"query", "context_menu"})

    def test_every_menu_press_cost_two_processes(self):
        """Nine presses, nine pairs -- this is the thing the trace was for."""
        menus = [group for group in self.duplicates if group["method"] == "context_menu"]
        self.assertEqual(len(menus), 9)
        self.assertTrue(all(group["count"] == 2 for group in menus))
        spreads = [group["spread_ms"] for group in menus]
        self.assertTrue(100 <= min(spreads) <= 150, spreads)
        self.assertTrue(100 <= max(spreads) <= 150, spreads)

    def test_typing_was_not_mistaken_for_duplicates(self):
        queries = [group for group in self.duplicates if group["method"] == "query"]
        self.assertEqual(queries, [])

    def test_a_menu_does_no_work_and_still_costs_the_startup(self):
        menus = [record for record in self.records if record["method"] == "context_menu"]
        for record in menus:
            self.assertLess(record["phases"]["ready_to_rendered"], 1.0)
            self.assertGreater(record["phases"]["creation_to_python_start"], 20.0)

    def test_a_query_spends_most_of_its_time_not_searching(self):
        queries = [record for record in self.records if record["method"] == "query"]
        fixed = sum(
            record["phases"]["creation_to_python_start"]
            + record["phases"]["entry_to_imports_done"]
            for record in queries
        ) / len(queries)
        search = sum(
            record["phases"]["rpc_to_ready"] + record["phases"]["ready_to_rendered"]
            for record in queries
        ) / len(queries)
        # Measured: 84 ms of start-up against 33 ms of searching, per query.
        self.assertGreater(fixed, search * 2, "start-up should dominate a query")


if __name__ == "__main__":
    unittest.main()
