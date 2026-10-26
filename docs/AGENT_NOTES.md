# AGENT_NOTES.md

Working notes for the agent (me). Human-facing docs live in `README.md`.

## 1. Repository layout (Browser History V2, a Flow Launcher plugin)

```
plugin.json              Flow plugin manifest (ID, version, entry point = run.py)
SettingsTemplate.yaml    Settings UI schema; every key here is read via self.settings
run.py                   Process entry point: Flow spawns `python run.py <json>` on EVERY keystroke
plugin/main.py           Flox subclass: __init__ (settings + browser discovery), query(), context_menu()
plugin/browsers.py       Browser profile discovery + SQLite access + HistoryItem
requirements.txt         flox-lib, pyperclip (installed into ./lib at packaging time)
tools/                   Agent tooling (this file's neighbours)
```

Key runtime fact: **Flow Launcher starts a fresh Python process for every keystroke.** There is
no long-lived state between queries, so anything "cached in memory" is worthless across
keystrokes; only on-disk caches survive. Per-query cost = process start + imports + init + query.

## 2. Turn start (harness can re-clone at any time)

```bash
bash tools/arena-turn-start.sh
```

After a re-clone, HEAD is knocked back to the branch's base commit while the remote still holds
this session's earlier commits. The script classifies the state and hard-resets only when local
is strictly behind. Recovery recipes:

- **Diverged** (local rebuilt on the base commit, remote holds earlier work):
  ```bash
  git diff --stat origin/arena/b80febd6-browser-history-v2   # confirm only intended changes
  git reset --soft origin/arena/b80febd6-browser-history-v2
  git commit -C HEAD@{1}
  ```
- **Tree incomplete / missing key files**: the snapshot restore lands files in waves. Wait, then
  re-run the script. `tools/test-turn-start.sh` self-tests the script against a throwaway clone.

## 3. Settings are validated by two people only: Flow and the user

`SettingsTemplate.yaml` keys are user-visible. Do not add, rename, or change the type of a key
without an agreed shape proposal — a renamed key silently resets the user's configuration.

## 4. Performance findings (2026-10-26)

Where the time goes for a single `query()` with 10 synthetic profiles (8 Chromium,
2 Firefox), measured with cProfile:

| stage | cost |
|---|---|
| `sqlite3.Cursor.execute` (all profiles) | **95 %+ of total** |
| `BrowserHistory.__init__` (settings + profile discovery) | ~0.5 ms |
| rows -> `HistoryItem`, de-dupe, sort, 500 x `strftime` | < 3 ms |
| copying one 58 MB history DB (only when it changed) | ~27 ms on Linux tmpfs |
| interpreter + `flox` import, per keystroke (Windows) | ~100 ms, a fixed floor |

So the work is entirely about which statements run. Two findings, both measured
with `tools/perf/bench_queries.py`:

1. **A time-ordered index walk cannot stop early when nothing matches.** It then
   visits every entry of the index. On Firefox that meant a row lookup per
   *visit* (`1086 ms` on 200k places) because the old query joined
   `moz_historyvisits`. On Chromium the walk costs a join per entry, which is why
   it loses to a plain scan even when the term matches: `66 ms` vs `13 ms` for a
   term that matches nothing, `15 ms` vs `20 ms` for one that matches well. Hence:
   ladder for Firefox, single scan for Chromium. `history_query.py` records this.
2. **A sequential table scan has bounded cost.** It is never the fastest plan but
   it is the only one whose cost is independent of what the search matches, and
   "nothing matches yet" is the state of the box while the user is still typing.

Baseline versus working tree, 2 Chromium + 2 Firefox profiles x 100k URLs,
medians of 7, on a 2-core sandbox:

| scenario | published | now | speedup |
|---|---|---|---|
| empty query | 98 ms | 29 ms | 3.4x |
| `youtube` | 158 ms | 39 ms | 4.1x |
| `python sqlite` | 210 ms | 51 ms | 4.1x |
| `zzqq` (no match) | 136 ms | 79 ms | 1.7x |

Chromium only (the setup on the owner's machine) is flatter: 1.1x on the empty
query, 1.6x on a common term, 1.7x on no-match. Most of what is left there is
interpreter start-up, not queries.

Reproduce with:

```bash
python tools/perf/bench_queries.py --profiles 2 --firefox 2 --urls 100000
python tools/perf/bench_queries.py --distribution flat      # pessimistic fixture
```

The benchmark runs each side in its own subprocess (as Flow Launcher does) and
extracts the published `plugin/browsers.py` from git, so the comparison is
against the real published code rather than a copy of it.

## 4a. Evidence from the owner's machine (runs 40349238896, 40350654072)

Windows 10, 28 cores, Python 3.13.14, sqlite 3.50.4. Real profiles: Edge (6
rows), Brave `Profile 0` (15,720 rows / 25.5 MB, the real one), Brave `Profile 1`
(56 rows). No Firefox installed, so Firefox ships for correctness and is measured
on synthetic data only.

### The keystroke, end to end (`tools/perf/measure_keystroke.py`)

| step | median |
|---|---|
| interpreter starts | 16.2 ms |
| + `flox` import | 57.9 ms |
| + this plugin's imports | 78.3 ms |
| **full keystroke, idle listing** | **122.1 ms** (500 results) |
| full keystroke, matching search | 128.8 ms (500 results) |
| full keystroke, no match yet | 120.6 ms (0 results) |

So **64 % of a keystroke is imports** and the plugin's own work is 42-51 ms.
`flox` alone is 41.7 ms of the imports and cannot be removed; whether `pyperclip`
(imported eagerly in `main.py`) can be deferred is unmeasured.

Most of that 42-51 ms is rendering: those measurements ran at the template's
default `max_ui_results` of 500. **The owner runs 25.** Rendering therefore costs
about 2 ms for them, leaving ~5 ms of plugin work after the database -- so at
their settings a keystroke is roughly 85 ms and almost entirely process start-up.
Measure with `--max-results 25` before optimising anything here.

Conclusion: **the runtime pass is finished.** There is no remaining query or
rendering work worth a change at this owner's settings; the floor is the
interpreter plus the launcher library.

### Query work per profile

| measurement | before | now |
|---|---|---|
| cold copy of the 25.5 MB database | 7.45 ms | 7.48 ms |
| cached copy check | 0.10 ms | 0.10 ms |
| Brave, idle listing | 11.24 ms | **1.85 ms** |
| Brave, frequent term | 7.89 ms | 8.28 ms |
| Brave, term matching nothing | 5.06 ms | 5.10 ms |
| all profiles, idle listing | 11.38 ms | **2.50 ms** |

The idle listing is the win: the recency walk fills the page in 1.85 ms instead of
sorting the whole `urls` table.

### Synthetic A/B on the same machine

2 Chromium + 2 Firefox profiles x 100k realistically wide URLs, medians of 5:
75 -> 21 ms (3.6x) idle, 131 -> 25 ms (5.2x) common, 128 -> 25 ms (5.2x) medium,
161 -> 34 ms (4.8x) multi-word, 116 -> 115 ms (1.0x) no-match. The no-match case
ends in the same full scan on both sides; it only improves where a Firefox-sized
history made the old query walk every visit.

40 plugin tests and 9 redaction tests pass on Windows 3.13 and in the sandbox.

## 4b. Rules we now hold the code to

- A plan that can be slower than what it replaces is not an optimisation. Every
  ladder rung is bounded by one full-history scan plus the windows it walked.
- Measure before enabling a ladder for a new engine; the Chromium comment exists
  so nobody re-enables it on intuition.
- Do not commit history content. Benchmarks use synthetic databases; any
  diagnostic against the real machine must print timings, sizes and index names
  only.

## 4c. Icon lookups: three ways to accidentally scan (2026-10-26)

Favicons are read from the browser's own store, and every mistake below looked
harmless in code review while costing more than the whole search it decorates.
All numbers: 25 URLs against a 20,000-site icon database, sandbox.

| written as | planned as | cost |
|---|---|---|
| `page_url LIKE 'https://site/%'` | SCAN (LIKE defeats the index unless case-sensitive) | 38.8 ms |
| one `OR` across 50 range pairs | SCAN | 25.5 ms |
| `icon_mapping JOIN favicon_bitmaps` | planner may drive from the bitmaps: every icon in the profile | 31.0 ms |
| one range seek per site, staged lookups | SEARCH on `page_url`, then on `icon_id`/`id` | 0.40 ms |

The lesson generalises: `EXPLAIN QUERY PLAN` the statement, not just the idea.
`tests/test_favicons.py::QueryShape` fails if a LIKE or a multi-OR lookup comes
back, and `tools/perf/bench_favicons.py` reproduces the table.

Final cost of the feature at 25 results: **+0.205 ms** warm (0.949 -> 1.154 ms
in the plugin, measured in-process by alternating the setting), 3.5 ms for the
one keystroke that fills the cache.

Beware the process-level A/B for effects this small: the same comparison by
spawning `run.py` reported +8 ms with a 14-20 ms spread, which is noise. If a
delta is under ~10 ms per process, measure it in-process.

## 4d. Matching may be measured, not guessed

`tools/perf/match_report.py` runs real searches against the real history and
sorts each into found / filtered / fuzzy / unmatchable / absent, where only
"fuzzy" is evidence for a new matcher. `--typos` reports how often one mistyped
character still finds the page (today: never -- the match is a substring test).

Flow Launcher does not re-rank plugin results by relevance: it orders by the
`Score` field plus the user's own history. In particular `add_item(score=...)`
is ours to set, and RPC plugins cannot call Flow's fuzzy matcher
(Flow-Launcher/Flow.Launcher#3022). So matching is entirely ours to change, and
needs no upstream PR.

## 4e. How a request actually runs (flox), and why eager work is a trap

`run.py` imports `plugin.main` and calls `BrowserHistory().run()` -- and `run()`
is empty. The RPC is executed by `flox.launcher.Launcher.__del__`, which calls
`__call__()` when the instance is collected at interpreter shutdown. So:

    __init__  ->  run() does nothing  ->  interpreter shutdown  ->  __del__  ->  the RPC

Two consequences:

* **Anything eager in `__init__` is paid by every request**, including a
  context menu, which reads no history and needs no browsers. Browser
  discovery and cache maintenance therefore live in `_ensure_ready()`, called
  from `query()` only.
* Work happens while the interpreter is tearing down. That is where the
  intermittent `import of ... halted; None in sys.modules` error seen in probes
  comes from. It is a flox frame, not this plugin's (do not chase it), but it is
  another reason not to add lazy imports that only run at the very end.

Modifying `sys.path`/imports at `__init__` time is fine; importing a module that
loads a Windows library (e.g. `pyperclip`) is deferred to the action that needs
it, so a keystroke or a menu never pays for the clipboard.

Measure both RPC shapes: `measure_keystroke.py` reports `menu` alongside
`idle`/`search`/`absent`, since the menu is the one request that must never be
slower than the listing it opens onto.

## 4f. The context menu, measured on the owner's machine (run 40358791830)

Inside one already-running process, 25 results, favicons on:

| phase | Windows 10, 28 cores |
|---|---|
| `BrowserHistory()` (flox settings + manifest + our settings) | 9.66 ms |
| discovery + cache upkeep (was in `__init__`) | 6.71 ms |
| one query, no favicons | 2.05 ms |
| one query, with favicons | 3.45 ms |
| the context menu body | 0.003 ms |

Whole-request medians from the same run: **menu 72.4 ms**, idle 110.5, search
116.9, absent 116.0. The menu is the cheapest request there is -- "plugin work
only" for it is negative, i.e. below the import floor, which is the interpreter
plus flox plus this plugin's imports (~64 % of any request, see 4a).

So: the menu was never slow *in this plugin*, before or after the fix (before
it, add the 6.7 ms of discovery). If it still feels slower than the listing, the
cause is outside the plugin's work -- Flow spawning the request again, or its
own menu rendering. `debug = true` in the plugin settings makes flox log
"Total time" per request, which shows whether one menu press runs twice.

Favicons cost **+1.4 ms** warm on this machine (2.05 -> 3.45), against +0.2 ms
on the sandbox fixture. Both are real; the difference is the size of the icon
index and the profile's icon database.

## 4g. The typing debounce is Flow's, and it is measurable (2026-10-26)

Sources read in Flow-Launcher/Flow.Launcher:

* `MainWindow.xaml.cs`: typing calls `Query(_settings.SearchQueryResultsWithDelay)`.
  Programmatic queries call `Query(false)`, so a delay only ever applies to typing.
* `Settings.SearchQueryResultsWithDelay` is global and **defaults to false**.
* When it is on: `plugin.Metadata.SearchDelayTime ?? Settings.SearchDelayTime`
  (`Settings.SearchDelayTime` defaults to **150 ms**), then `await Task.Delay(...)`.

Consequences:

* `SearchDelayTime: 0` in `plugin.json` is a per-plugin **default** that wins over
  the global 150 ms, because `??` only falls back on null. Anyone can override it
  per plugin in Flow's settings UI.
* An existing install keeps its saved `SearchDelayTime` (often null), so declaring
  the value in `plugin.json` affects new installs and the UI's "default"; for an
  existing install the user sets it in Flow's UI.
* **A plugin setting cannot do this.** The delay happens before the process is
  spawned, so by the time `main.py` runs the wait is already spent. Do not offer
  an "instant search" checkbox that pretends otherwise.

## 4h. Where a request's milliseconds actually are (owner machine, run 40361518976)

Traced through the real entry point on the portable tree, 25 results, tracing on.
Medians in ms:

| phase | query | menu |
|---|---|---|
| OS process created -> Python starts | 34.1 | 32.7 |
| run.py start -> imports done | 40.5 | 40.0 |
| imports -> plugin constructed | 0.7 | 0.4 |
| constructed -> RPC begins (flox runs it at shutdown) | 0.0 | 0.0 |
| RPC -> discovery done | 28.1 | 0.0 |
| discovery -> results built | 7.4 | 0.1 |
| **whole process** | **76.7** | **40.5** |

The menu does **0.1 ms of work** and still costs 40.5 ms, because 73 ms of a
query and 40 ms of a menu are the same fixed start-up. A third of it is spent
before Python exists (`GetProcessTimes` -> first line of run.py): process
creation, the antivirus scan of the executable, and interpreter boot.

This is the floor for a JSON-RPC plugin: Flow spawns a process per request, so
nothing inside the plugin can go below it. Only two things can move it -- fewer
bytes to import, or Defender exclusions for the interpreter and plugin folder.

To see this on the real launcher: turn on "Trace Requests", use Flow for a
minute, then `python tools/perf/read_trace.py "<...>/Settings/Plugins/Browser History V2/requests.jsonl"`.
`tools/perf/watch_launches.ps1` records the same launches from outside, which is
how to tell one press from two.

## 4i. The context menu is asked for twice per press (owner trace, 2026-10-26)

The owner's trace (Flow 2.1.4, Windows 10) is the fixture
`tests/fixtures/trace-owner-windows-flow-2.1.4.jsonl`: 52 requests over about a
minute, no search text or URLs in it.

| | query | menu |
|---|---|---|
| requests | 34 | 18 (**nine pairs**) |
| OS created -> Python starts | 36.6 | 37.2 |
| imports done | 46.4 | 46.5 |
| RPC -> discovery done | 24.1 | 0.0 |
| discovery -> rendered | 8.7 | 0.2 |
| whole process | 79.5 | 47.2 |

**Every one of nine menu presses cost two processes**, 101-139 ms apart (median
121). A person cannot open two menus in that time, and both returned the same
two entries, so Flow asked twice for one press. That is the answer to "why does
the menu feel slower than the list": the list is one process of 79.5 ms, the
menu is two processes of 47.2 ms, ~121 ms apart.

It is not a timeout retry on our side: each menu process finished in ~47 ms
while the second did not start until ~121 ms, so the first had already answered.

`read_trace.duplicates()` encodes this, and
`tests/test_trace_fixture.py` asserts the nine pairs so the evidence cannot be
lost. Next step is upstream, not here: Flow spawns two context_menu requests per
press. Nothing a plugin does can merge them.

## 4j. Answering before teardown: 10 ms on Linux, ~3 ms on Windows

`flox` builds the answer from `__del__`, so a request completes during
interpreter shutdown. `run()` now calls the same library entry (`Flox.call()`)
and exits immediately, which drops the shutdown work.

| | before | after |
|---|---|---|
| sandbox, query wall | 50.6 ms | 40.1 ms |
| sandbox, menu wall | ~51 ms | 38.6 ms |
| owner runner, query wall | 110.5 ms | 107.3 ms |
| owner runner, menu wall | 72.4 ms | 71.3 ms |

Windows tears the interpreter down in ~2-3 ms, so the win there is small; keep
the change because it sends the JSON ~20 ms earlier and stops depending on
shutdown order, not because of the wall-clock saving.

## 4k. What is left, and what is not fixable in this repository

Per menu request: 37 ms before Python exists (interpreter boot + antivirus scan
of the executable), 41 ms of imports (of which `flox` is the bulk and cannot be
removed), 0.4 ms of construction, and 0.15 ms of the plugin's own work.

Levers that remain:

* **Defender exclusions** for the Python interpreter and the plugin folder --
  attacks the 37 ms before Python starts, which is the largest single slice.
  User-side, and testable by A/B on their own machine.
* **Fewer imports** -- our own modules are a few milliseconds of the 41 ms; the
  bulk is `flox`, which the plugin cannot do without.
* **Upstream**: one context_menu request per press instead of two (4i).

Not a lever: anything inside the plugin's query or menu code, which together are
under 9 ms of a 47 ms menu.

## 5. Measurement discipline

Tests must pass on Windows as well as here. One trap already paid for: a
`tempfile.TemporaryDirectory` cannot be removed while the process' working
directory is inside it (`WinError 32`), and `addCleanup` runs last-registered
first -- so a chdir-based test must restore the directory *before* the cleanup
runs. Better: do not chdir at all. Redirect `plugin.cache_dir` instead.

Findings only count if they reproduce on the user's machine. Sandbox numbers are synthetic (2
cores, Linux, fabricated history); use the local Windows runner for a verdict. See
`tools/github_actions_runner/AGENTS.md` for dispatch mechanics.
