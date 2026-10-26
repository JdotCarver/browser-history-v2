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
