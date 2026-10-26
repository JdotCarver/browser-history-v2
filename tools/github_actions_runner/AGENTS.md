# AGENTS.md — GitHub Actions runner bridge

Use this runbook whenever a task needs GitHub-hosted or local Windows Actions.
The scripts are the interface; do not improvise a second dispatch/log mechanism.

## Hard constraints discovered in Arena

- `git push`, repository contents, run metadata, `gh run view`, and `gh run watch` work.
  For agent-facing waits, use the quieter `watch_run.sh` rather than repeated
  `gh run watch` frames.
- `gh workflow run` is denied by the harness token. Use `request_run.sh`, which
  dispatches through a push instead.
- Runner administration/registration is denied in Arena. Run the PowerShell
  launcher only on the user's Windows machine. Never request its token in chat.
- The launcher resolves binaries from the global `actions/runner` latest release,
  not the target repository's runner catalog. A new repository can return an
  empty catalog until its first workflow run. Keep registration tokens
  repository-scoped and never auto-dispatch a workflow to initialize Actions.
- `gh run view --log` and artifact downloads follow blocked signed URLs. Use the
  workflow's Export logs job and `fetch_logs.sh`; do not treat this as a metadata
  API failure.
- Pass `--repo OWNER/REPO` in ad hoc `gh run` commands. In a fork checkout, an
  implicit repository can resolve to upstream and produce a misleading 404.
- Never install a runner in the Arena sandbox. The runner belongs on the user's
  machine.

## Request a GitHub-hosted Windows run

1. Commit the code to test. `request_run.sh` deliberately refuses a dirty tree,
   so it cannot silently test a different commit.
2. From the repository root:

   ```bash
   bash tools/github_actions_runner/request_run.sh cloud-windows
   ```

3. The script commits only `.github/runner-request.toml`, pushes the current
   branch, finds the run by commit SHA, watches it, and fetches logs into
   `.runner-logs/<run-id>/`.
4. Read `run.json` and the `02-*.log` workload file. A successful receipt has
   `"runner_environment": "github-hosted"`.

## Request a local Windows run

1. Commit the code to test, then run:

   ```bash
   bash tools/github_actions_runner/request_run.sh local-windows "Performance Pass Greenlight"
   ```

   The optional second argument becomes the workload job name. Omit it for the
   backward-compatible default, `Windows runner probe`.

2. Read the fetched workload log. A genuine local receipt has
   `"runner_environment": "self-hosted"`; do not claim local validation from a
   queued run or from the cloud planner/exporter jobs.

A local request requires an already available runner with the configured
self-hosted labels. If no matching runner is online, GitHub queues the job
instead of failing immediately. Report that state; do not change labels or add
fallback logic merely because the job is queued. The default watch timeout is
30 minutes; override it with `RUNNER_WATCH_TIMEOUT_MINUTES` when the workload
justifies a longer wait.

## Concurrency and obsolete runs

- The workflow cancels older runs only in the same workflow and branch. A newer
  request supersedes an older one; independent workflows and branches remain
  parallel.
- `request_run.sh` watches its exact commit SHA. Exit code 3 means that run was
  canceled, normally because a newer request superseded it. Do not treat that as
  a workload failure, and do not silently attach to another run.
- After cancellation, inspect the printed latest requests or use
  `gh run list --repo OWNER/REPO --workflow runner-request.yml --limit 5`. Only
  the latest request is authoritative for the current tree.
- Do not copy this cancellation policy blindly to side-effecting production
  workflows; partial uploads/external writes need a separate design.

## Recover an interrupted run

1. Find the request run without downloading logs:

   ```bash
   gh run list --repo OWNER/REPO --workflow runner-request.yml --limit 10
   gh run view RUN_ID --repo OWNER/REPO --json status,conclusion,url,jobs
   ```

2. If still active, use `bash tools/github_actions_runner/watch_run.sh RUN_ID`.
   It prints only status transitions. When complete:

   ```bash
   bash tools/github_actions_runner/fetch_logs.sh RUN_ID
   ```

3. If the run completed but no logs exist, inspect its **Export logs** job in the
   web UI. The exporter cannot download its own still-running log; this is the
   intentional same-run limitation of the PoC.

## Safety and scope

- The probe must remain non-sensitive: no `.env`, Twitch, Notion, YouTube, or
  personal access tokens.
- The workflow is push-only. Do not add `pull_request` or other untrusted-code
  triggers to a self-hosted runner workflow.
- An open/close foreground session limits listening time; it is not a security
  sandbox. It runs with the signed-in Windows user's permissions while active,
  and registration credentials persist between sessions.
- Do not add automatic local-to-cloud fallback to jobs with external side
  effects. A queued local job can later run and duplicate those effects.
- To adapt the workload, replace the probe step in `runner-request.yml`; keep the
  explicit planner and log exporter unless a new design is agreed first.

See `README.md` in this directory for architecture, contracts, and validation.
