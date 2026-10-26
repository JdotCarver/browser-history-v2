#!/usr/bin/env bash
# request_run.sh
# Request one explicit cloud/local run, watch it, and fetch its exported logs.

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || [[ "$1" != "cloud-windows" && "$1" != "local-windows" ]]; then
    echo "Usage: bash tools/github_actions_runner/request_run.sh cloud-windows|local-windows [JOB_NAME]" >&2
    exit 2
fi

TARGET="$1"
if [ "$#" -eq 2 ]; then
    JOB_NAME="$2"
else
    JOB_NAME="Windows runner probe"
fi
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPOSITORY_ROOT"

if [ -n "$(git status --porcelain)" ]; then
    echo "The working tree must be clean before requesting a run." >&2
    echo "Commit or stash your work first so the run tests exactly what you see." >&2
    exit 2
fi

BRANCH="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
    echo "A detached HEAD cannot be used for a push-triggered runner request." >&2
    exit 2
fi

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch origin "$BRANCH"
    BEHIND="$(git rev-list --count "HEAD..origin/${BRANCH}")"
    if [ "$BEHIND" -gt 0 ]; then
        echo "This branch is ${BEHIND} commit(s) behind origin/${BRANCH}." >&2
        echo "Sync first; refusing to overwrite or run stale work." >&2
        exit 2
    fi
fi

REQUEST_PATH=".github/runner-request.toml"
python - "$SCRIPT_DIRECTORY" "$REQUEST_PATH" "$TARGET" "$JOB_NAME" <<'PY'
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from select_runner import validate_job_name

try:
    job_name = validate_job_name(sys.argv[4])
except ValueError as error:
    raise SystemExit(f"Invalid runner request: {error}") from error


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


request_path = Path(sys.argv[2])
request_path.write_text(
    "# runner-request.toml\n"
    "# Written by tools/github_actions_runner/request_run.sh.\n"
    f"request_id = {toml_string(str(uuid.uuid4()))}\n"
    f"target = {toml_string(sys.argv[3])}\n"
    f"job_name = {toml_string(job_name)}\n",
    encoding="utf-8",
)
PY

python "$SCRIPT_DIRECTORY/select_runner.py" "$REQUEST_PATH" >/dev/null
git add -- "$REQUEST_PATH"
git commit -m "Actions: request ${TARGET} run"
COMMIT="$(git rev-parse HEAD)"
git push -u origin "HEAD:${BRANCH}"

# Tie API reads to the same repository that received the push. In a fork
# checkout, an implicit `gh repo view` can otherwise select the upstream.
ORIGIN_URL="$(git remote get-url origin)"
REPOSITORY="$(gh repo view "$ORIGIN_URL" --json nameWithOwner --jq .nameWithOwner)"
export RUNNER_REPOSITORY="$REPOSITORY"
WORKFLOW_PATH=".github/workflows/runner-request.yml"
RUN_ID=""

printf '\nWaiting for GitHub to create the run for commit %s...\n' "${COMMIT:0:12}"
for _ in {1..30}; do
    RUN_IDS="$(gh api "repos/${REPOSITORY}/actions/runs?head_sha=${COMMIT}&event=push&per_page=100" \
        --jq ".workflow_runs[] | select(.path == \"${WORKFLOW_PATH}\") | .id")"
    if [ -n "$RUN_IDS" ]; then
        RUN_ID="${RUN_IDS%%$'\n'*}"
        break
    fi
    sleep 2
done

if [ -z "$RUN_ID" ]; then
    echo "No ${WORKFLOW_PATH} run appeared for ${COMMIT}." >&2
    echo "Check whether the workflow exists on this branch and Actions is enabled." >&2
    exit 1
fi

RUN_URL="$(gh run view "$RUN_ID" --repo "$REPOSITORY" --json url --jq .url)"
printf '\nRun %s: %s\n' "$RUN_ID" "$RUN_URL"
printf 'Workload name: %s\n' "$JOB_NAME"
if [[ "$TARGET" == "local-windows" ]]; then
    echo "The local job needs your foreground Windows runner session to be open."
fi

WATCH_TIMEOUT_MINUTES="${RUNNER_WATCH_TIMEOUT_MINUTES:-30}"
set +e
bash "$SCRIPT_DIRECTORY/watch_run.sh" "$RUN_ID"
WATCH_EXIT=$?
set -e
if [ "$WATCH_EXIT" -eq 124 ]; then
    echo "Run ${RUN_ID} is still active after ${WATCH_TIMEOUT_MINUTES} minutes." >&2
    echo "For a local run, check the Windows runner session console. ${RUN_URL}" >&2
    exit 2
fi
if [ "$WATCH_EXIT" -ne 0 ]; then
    echo "Could not watch run ${RUN_ID} (gh exit ${WATCH_EXIT}). ${RUN_URL}" >&2
    exit "$WATCH_EXIT"
fi

CONCLUSION="$(gh run view "$RUN_ID" --repo "$REPOSITORY" --json conclusion --jq .conclusion)"
if [ "$CONCLUSION" = "cancelled" ]; then
    echo "This request was canceled, normally because a newer request superseded it." >&2
    echo "It is not an authoritative workload result. Latest requests on ${BRANCH}:" >&2
    gh run list --repo "$REPOSITORY" --workflow runner-request.yml --branch "$BRANCH" --limit 3
    exit 3
fi
if ! bash "$SCRIPT_DIRECTORY/fetch_logs.sh" "$RUN_ID"; then
    echo "The run completed, but its logs were not exported. Inspect Export logs: ${RUN_URL}" >&2
    exit 1
fi

printf '\nRun URL: %s\n' "$RUN_URL"
if [ "$CONCLUSION" != "success" ]; then
    echo "Read the fetched workload log above for the failure." >&2
    exit 1
fi
