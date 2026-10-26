# README.md — Repository-agnostic GitHub Actions runner bridge

This folder provides one explicit workflow for GitHub-hosted Windows and a
foreground Windows work-session runner on a user-owned machine. It is designed
around the Arena harness's actual limits rather than around PAT workarounds that
cannot solve blocked downloads.

## Architecture

```text
Arena: request_run.sh
  |  commit + push runner-request.toml
  v
GitHub: Select runner (small Ubuntu job)
  |  validates target; emits runs-on labels
  +------------------------+------------------------+
  |                                                 |
  v                                                 v
cloud-windows                                 local-windows
windows-latest                    self-hosted + arena-session
  |                                                 |
  +------------------------+------------------------+
                           |
                           v
              Export logs (small Ubuntu job)
                           |  completed job logs + run metadata
                           v
                 actions-logs git branch
                           |
                           v
Arena: .runner-logs/<run-id>/ via fetch_logs.sh
```

There is no automatic fallback. `target` is always explicit:

```toml
# runner-request.toml
request_id = "2026-10-24T14:20:00+02:00"
target = "local-windows" # or "cloud-windows"
job_name = "Performance Pass Greenlight"
```

The request scripts generate a UUID for `request_id`; a timestamp is also valid
when writing a request manually. `job_name` is optional and becomes the workload
job's display name; omitted, it defaults to `Windows runner probe`.

## Concurrency: latest request wins

Actions triggers per **push**, not per commit. Several commits in one push create
one run; several quick pushes create several runs. The workflow uses:

```yaml
concurrency:
  group: runner-request-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

A newer request therefore cancels an obsolete pending/active request only for
the same workflow and branch. Independent workflows and other branches remain
parallel. Jobs inside one run still follow their explicit `needs` ordering.

`request_run.sh` always watches the run matching its exact commit SHA. If that
run is canceled, it exits with code 3 and prints the latest requests instead of
switching to a different run or presenting the cancellation as a test failure.
An obsolete canceled run may have incomplete logs; do not use it as an
authoritative result.

This latest-wins policy is appropriate for the non-sensitive probe. Revisit it
before reusing it for uploads, Notion writes, or other side-effecting workloads,
where cancellation can leave partial external state.

## Why this shape

| Harness operation | Verified result | Consequence |
|---|---|---|
| Push code/workflow files | Works | A push is the dispatch channel. |
| Read workflow files on any branch | Works through contents/git APIs | No raw.githubusercontent.com dependency. |
| List/view/watch runs and jobs | Works | Status needs no bridge. |
| `gh workflow run` | 403: Actions write denied | Do not rely on workflow_dispatch from Arena. |
| List/register runners | 403: Administration denied | Runner administration is outside Arena's control plane. |
| `gh run view --log` | API starts, signed download fails | Return logs through git instead. |
| Download artifacts | API starts, Azure Blob download fails | Do not use artifacts as the Arena log bridge. |

A PAT in Arena would not solve the signed-URL network blocks.

## Request runs from Arena

When a matching self-hosted runner is already available:

```bash
bash tools/github_actions_runner/request_run.sh local-windows "Performance Pass Greenlight"
```

The optional second argument sets `job_name`. For a cloud-only run, no local
listener is needed:

```bash
bash tools/github_actions_runner/request_run.sh cloud-windows
```

Both commands require a clean tree. They push a small request commit, print the
run URL/ID, wait up to 30 minutes, and save logs under `.runner-logs/<run-id>/`.
Set `RUNNER_WATCH_TIMEOUT_MINUTES` for a different watch limit and
`RUNNER_POLL_INTERVAL_SECONDS` for the polling cadence. The watcher prints only
status transitions; use `watch_run.sh RUN_ID` for the same quiet wait during
recovery. Planner/export jobs are pinned to `ubuntu-24.04` so Ubuntu migration
annotations do not repeatedly enter agent context.

## Log contract and recovery

The same-run exporter downloads all completed jobs except itself and publishes:

```text
runs/<run-id>/run.json
runs/<run-id>/runner-request.toml
runs/<run-id>/01-Select-runner-<job-id>.log
runs/<run-id>/02-<job-name>-<job-id>.log
```

This includes planner and workload output on success or failure. ANSI terminal
control sequences are removed and line endings normalized for readable plain-text
logs. `run.json` is captured while the exporter is still active and says so
explicitly. The exporter cannot include its own log. If a future design needs
that final log too, a separate `workflow_run` relay can export the complete run,
but its workflow must exist on the default branch.

To retrieve a known run again:

```bash
bash tools/github_actions_runner/fetch_logs.sh RUN_ID
```

The log branch is expected to accumulate history in this PoC; add a retention
policy before using it for high-volume production workloads. Repository Actions
workflow permissions must allow `contents: write` for the export job.

## Security boundary

- Keep self-hosted workflows on private repositories and trusted push events.
  Do not add forked `pull_request` triggers.
- The runner needs outbound connectivity, not an exposed inbound port. The main
  risk is workflow code executing with the Windows account's permissions while
  the runner is listening.
- An open/close foreground session limits listening time; it is not a security
  sandbox. Registration credentials persist between sessions. The PoC uses no
  repository secrets or local `.env`; production credentials require a separately
  agreed security design.
- The checkout runs under the session runner's `_work`, separate from the daily
  source clone.

## Validation baseline

Validated on 2026-10-24:

- Ten contract tests, Ruff lint/format, Python compilation, shell syntax, and
  diff checks pass.
- Cloud run `40105503606` completed successfully through `request_run.sh`;
  `fetch_logs.sh` retrieved normalized planner/workload logs from `actions-logs`.
  Its Windows PowerShell 7 self-test confirmed multi-line runner-download JSON
  is reconstructed correctly.
- Concurrency was dogfooded with two rapid requests: run `40105491999` was
  canceled as obsolete and its watcher exited with the documented code 3;
  latest run `40105503606` succeeded.
- Run `40106748416` validated quiet status-only watching and pinned
  `ubuntu-24.04` planner/export jobs; the completed run had zero annotations.
- Run `40209997148` validated a custom `job_name`: GitHub metadata and the
  self-hosted receipt both reported `Runner Job Naming Dogfood`, and the exported
  workload filename used the same name.
- Local runs `40106210539` and `40106289003` both completed successfully against
  the same open Windows foreground session. Their receipts confirmed
  `self-hosted`, and the second run proved that no relaunch is needed between
  jobs.

## Copy this PoC to another repository

Copy these paths unchanged:

```text
.github/workflows/runner-request.yml
.github/runner-request.toml
tools/github_actions_runner/
```

Add `.runner-logs/` to the repository's local ignore rules (the fetch script
also adds it to `.git/info/exclude`), and point the root `AGENTS.md` at
`tools/github_actions_runner/AGENTS.md`. Replace only the probe workload with
the new repository's commands; leave target selection and log export intact.
