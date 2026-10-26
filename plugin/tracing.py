# plugin/tracing.py
"""Opt-in recording of what each request actually costs.

Flow Launcher runs this plugin as a new process for every keystroke and every
context menu, so a request is: interpreter start -> imports -> construction ->
(flox runs the RPC at interpreter shutdown) -> our work -> exit. Timing it from
outside cannot separate those, so this writes one line per request instead.

Off unless the user asks for it. When on, it records numbers and the RPC method
name and nothing else: no query text, no URLs, no titles. The file is a ring of
the most recent requests, so it cannot grow without bound.

Read it with `tools/perf/read_trace.py`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

# Enough to cover a few minutes of deliberate testing, small enough to ignore.
MAX_LINES = 5000
TRIM_TO = 2000

# The process' start, set by run.py before it imports anything.
_PROCESS_MONO_ENV = "BH_PROCESS_MONO"
_PROCESS_EPOCH_ENV = "BH_PROCESS_EPOCH"
_PROCESS_CREATED_ENV = "BH_PROCESS_CREATED"


def mark_process_start() -> None:
    """Called as the very first thing the entry point does."""
    os.environ[_PROCESS_MONO_ENV] = repr(time.perf_counter())
    os.environ[_PROCESS_EPOCH_ENV] = repr(time.time())
    creation = _process_creation_epoch()
    if creation is not None:
        os.environ[_PROCESS_CREATED_ENV] = repr(creation)


def _process_creation_epoch() -> Optional[float]:
    """When the operating system created this process, in epoch seconds.

    The gap between this and the first line of Python is the interpreter boot
    plus whatever the antivirus did to the executable on the way -- the part of
    a request that no Python code can measure from the inside, because it is
    still happening before the code exists.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Without these the default 32-bit restype truncates the pseudo-handle
        # on a 64-bit process, and the call fails with an invalid handle.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        handle = kernel32.GetCurrentProcess()
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            os.environ["BH_CREATED_ERROR"] = f"GetProcessTimes failed ({ctypes.get_last_error()})"
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000 - 116_444_736_00  # 1601 -> 1970
    except Exception as error:  # noqa: BLE001 - any failure here is non-fatal
        os.environ["BH_CREATED_ERROR"] = f"{type(error).__name__}: {error}"
        return None


def _epoch_at_entry() -> Optional[float]:
    try:
        return float(os.environ[_PROCESS_EPOCH_ENV])
    except (KeyError, ValueError):
        return None


def _wall_gap_ms(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None:
        return None
    return round((end - start) * 1000, 3)


def process_creation_epoch() -> Optional[float]:
    try:
        return float(os.environ[_PROCESS_CREATED_ENV])
    except (KeyError, ValueError):
        return None


def process_start_mono() -> Optional[float]:
    try:
        return float(os.environ[_PROCESS_MONO_ENV])
    except (KeyError, ValueError):
        return None


def _ms_since(moment: Optional[float], other: Optional[float]) -> Optional[float]:
    """Milliseconds between two monotonic readings, or None if either is missing."""
    if moment is None or other is None:
        return None
    return round((other - moment) * 1000, 3)


class RequestTrace:
    """Timestamps for one request, written out once at the end."""

    def __init__(
        self,
        path: Path,
        enabled: bool,
        import_mono: Optional[float] = None,
        entry_mono: Optional[float] = None,
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.started = time.perf_counter()
        # Two moments that happen before this object exists: the first line of
        # run.py, and the end of this module's imports. Passed in rather than
        # guessed, because the gap between them is the startup cost.
        self.import_mono = import_mono
        self.entry_mono = entry_mono
        self.marks: Dict[str, float] = {}

    def mark(self, name: str) -> None:
        if self.enabled and name not in self.marks:
            self.marks[name] = time.perf_counter()

    def finish(self, method: str, **extra) -> None:
        """Append this request to the trace. Never raises."""
        if not self.enabled:
            return
        try:
            ended = time.perf_counter()
            start = process_start_mono()
            constructed = self.marks.get("constructed")
            phases = {
                "creation_to_python_start": _wall_gap_ms(
                    process_creation_epoch(), _epoch_at_entry()
                ),
                "entry_to_imports_done": _ms_since(self.entry_mono, self.import_mono),
                "imports_to_constructed": _ms_since(self.import_mono, constructed),
                "constructed_to_rpc": self._between("constructed", "rpc"),
                "rpc_to_ready": self._between("rpc", "ready"),
                "ready_to_rendered": self._between("ready", "rendered"),
                "total_since_process_start": _ms_since(start, ended),
            }
            record = {
                "ts": round(float(os.environ.get(_PROCESS_EPOCH_ENV, time.time())), 3),
                "pid": os.getpid(),
                "method": method,
                "flow": extra.pop("flow", ""),
                "results": extra.pop("results", None),
                "phases": {key: value for key, value in phases.items() if value is not None},
            }
            record.update(extra)
            self._append(record)
        except Exception:
            # A trace must never be the reason a search fails.
            pass

    def _between(self, first: str, second: str) -> Optional[float]:
        start = self.marks.get(first)
        end = self.marks.get(second)
        if start is None or end is None:
            return None
        return round((end - start) * 1000, 3)

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

        if self._line_count() > MAX_LINES:
            self._trim()

    def _line_count(self) -> int:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def _trim(self) -> None:
        """Keep the newest TRIM_TO lines: a debugging aid, not an archive."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.writelines(lines[-TRIM_TO:])
        except OSError:
            pass
