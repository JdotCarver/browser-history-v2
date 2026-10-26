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

## 4. Performance baseline (2026-10-26, sandbox, synthetic 200k-row DBs)

Where the time goes for a single `query()` with 10 profiles (8 Chromium, 2 Firefox):

| stage | cost |
|---|---|
| `BrowserHistory.__init__` (settings + profile discovery) | ~0.5 ms |
| `sqlite3.Cursor.execute` (all profiles, serial) | **95 %+ of total** |
| row → `HistoryItem`, de-dupe, sort, 500 × `strftime` | < 3 ms |
| copy of one 58 MB history DB (only when it changed) | ~27 ms (Linux tmpfs) |
| interpreter + `flox` import (Windows, per keystroke) | ~100 ms (fixed floor) |

Scenarios, current code, serial (median of 5):

| query | current |
|---|---|
| `""` | 187 ms |
| `"github"` | 364 ms |
| no matches (`"xqz"`) | 2606 ms |

The zero-match case is the cliff: `ORDER BY <time> DESC LIMIT n` over an index cannot stop early
when nothing matches, so it walks the whole history doing random row lookups. Typing the first
letters of a rare word is exactly that state.

## 5. Measurement discipline

Findings only count if they reproduce on the user's machine. Sandbox numbers are synthetic (2
cores, Linux, fabricated history); use the local Windows runner for a verdict. See
`tools/github_actions_runner/AGENTS.md` for dispatch mechanics.
