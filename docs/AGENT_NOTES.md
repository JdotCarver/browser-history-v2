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

So **64 % of a keystroke is imports**, and the plugin's own work is 42-51 ms.
Within that work the database is now 2.5 ms (idle) to 8 ms (a matching search),
which means **rendering 500 items plus init and teardown is the next target, not
SQL**. `flox` alone is 41.7 ms of the imports and cannot be removed; whether
`pyperclip` (imported eagerly in `main.py`) can be deferred is unmeasured.

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

## 5. Measurement discipline

Findings only count if they reproduce on the user's machine. Sandbox numbers are synthetic (2
cores, Linux, fabricated history); use the local Windows runner for a verdict. See
`tools/github_actions_runner/AGENTS.md` for dispatch mechanics.
