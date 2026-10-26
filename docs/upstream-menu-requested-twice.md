# Draft issue for Flow-Launcher/Flow.Launcher

Title: JSON-RPC plugin receives two `context_menu` requests per menu press, ~121 ms apart

---

## What happens

Opening the context menu on a result from a JSON-RPC (Python) plugin starts the
plugin's `run.py` **twice** for one press. Both processes receive
`{"method": "context_menu"}` and both return the same entries, so the plugin does
its work twice and the user waits for both.

This is invisible from the plugin side of a single request: each process looks
healthy. It only shows when the requests are timestamped together.

## Evidence

Flow Launcher 2.1.4, Windows 10, a JSON-RPC plugin, one minute of ordinary use.
52 requests total: 34 `query`, 18 `context_menu`.

- The 18 menu requests are **nine pairs, and nothing else**. Every press produced
  exactly two processes.
- Gap inside a pair: 101, 117, 118, 121, 121, 125, 131, 137, 139 ms (median 121).
- Each process takes ~47 ms from creation to writing its response, so the first
  one has already answered before the second starts.
- Both processes return the same two context-menu entries.
- No `query` request was ever duplicated: gaps between queries were 118-300 ms,
  which is typing cadence.

Per-press cost, from the plugin's own trace:

| phase | query | context_menu |
|---|---|---|
| process created -> Python starts | 36.6 ms | 37.2 ms |
| imports done | 46.4 ms | 46.5 ms |
| plugin's own work | 32.8 ms | 0.2 ms |
| whole process | 79.5 ms | 47.2 ms |

So one menu press costs about 121 ms (gap) + 47 ms = **~168 ms**, more than a
whole history search (79.5 ms) — even though the menu itself does 0.2 ms of work.

## Why I think it is Flow and not the plugin

- `MainViewModel.QueryContextMenu()` is reached from **two** entry points that
  both call it: `Query(bool searchDelay, ...)` (the `ContextMenuSelected()`
  branch) and `QueryAsync(...)` (the same branch). `EnterContextMenu()` itself
  only sets `SelectedResults = ContextMenu`, so it does not spawn anything.
- The plugin cannot merge two RPC calls: the second process starts after the
  first has exited.
- A retry-on-timeout would not explain it: the first process answered in ~47 ms,
  and the second did not start until ~121 ms.

## Reproducing

Enable the plugin's request tracing, open the context menu ~10 times, then look
at the timestamps in `requests.jsonl`: two `context_menu` entries 100-140 ms
apart for each press. Anything similar (a log line per RPC in the JSON-RPC host)
should show the same doubling.

## Suggestion

Have the context-menu path query each plugin once per press. If both `Query`
and `QueryAsync` can fire for one user action, `QueryContextMenu()` could be
guarded (e.g. skip if a menu request for the same selected result is already in
flight), or whichever path is the duplicate could stop calling it.

## Environment

- Flow Launcher 2.1.4
- Windows 10 (patch level withheld), Python 3.13.14
- Plugin is JSON-RPC/Python, returning two context-menu entries
