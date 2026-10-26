#!/usr/bin/env bash
# fetch_logs.sh
# Fetch one run's exported logs without checking out another branch.

set -euo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9]+$ ]]; then
    echo "Usage: bash tools/github_actions_runner/fetch_logs.sh RUN_ID" >&2
    exit 2
fi

RUN_ID="$1"
REPOSITORY_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPOSITORY_ROOT"
ORIGIN_URL="$(git remote get-url origin)"
REPOSITORY="$(gh repo view "$ORIGIN_URL" --json nameWithOwner --jq .nameWithOwner)"

echo "Fetching the actions-logs branch for run ${RUN_ID}..."
git fetch origin actions-logs:refs/remotes/origin/actions-logs

LOG_PREFIX="runs/${RUN_ID}"
if ! git ls-tree --name-only "origin/actions-logs" "$LOG_PREFIX" | grep -q .; then
    echo "No exported logs found at ${LOG_PREFIX}." >&2
    echo "Check the run's Export logs job: gh run view ${RUN_ID} --repo ${REPOSITORY}" >&2
    exit 1
fi

DESTINATION=".runner-logs/${RUN_ID}"
mkdir -p "$DESTINATION"

# Keep downloaded logs out of the source commit without requiring a root .gitignore edit.
LOCAL_EXCLUDE="$(git rev-parse --git-path info/exclude)"
if ! grep -qxF '.runner-logs/' "$LOCAL_EXCLUDE" 2>/dev/null; then
    printf '\n# GitHub Actions runner log exports\n.runner-logs/\n' >>"$LOCAL_EXCLUDE"
fi

git archive "origin/actions-logs" "$LOG_PREFIX" |
    tar -xf - -C "$DESTINATION" --strip-components=2

printf '\nLogs for run %s are in %s:\n' "$RUN_ID" "$DESTINATION"
find "$DESTINATION" -type f -print | sort
