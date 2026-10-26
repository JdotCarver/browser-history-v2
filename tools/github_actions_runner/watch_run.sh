#!/usr/bin/env bash
# watch_run.sh
# Wait for a GitHub Actions run while printing only status transitions.

set -euo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9]+$ ]]; then
    echo "Usage: bash tools/github_actions_runner/watch_run.sh RUN_ID" >&2
    exit 2
fi

RUN_ID="$1"
POLL_INTERVAL_SECONDS="${RUNNER_POLL_INTERVAL_SECONDS:-10}"
WATCH_TIMEOUT_MINUTES="${RUNNER_WATCH_TIMEOUT_MINUTES:-30}"
if [[ ! "$POLL_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "RUNNER_POLL_INTERVAL_SECONDS must be a positive integer." >&2
    exit 2
fi
if [[ ! "$WATCH_TIMEOUT_MINUTES" =~ ^[1-9][0-9]*$ ]]; then
    echo "RUNNER_WATCH_TIMEOUT_MINUTES must be a positive integer." >&2
    exit 2
fi

# request_run.sh exports the push repository; standalone recovery uses origin.
REPOSITORY="${RUNNER_REPOSITORY:-}"
if [ -z "$REPOSITORY" ]; then
    ORIGIN_URL="$(git remote get-url origin)"
    REPOSITORY="$(gh repo view "$ORIGIN_URL" --json nameWithOwner --jq .nameWithOwner)"
fi
if [[ ! "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "Could not resolve owner/repo from origin: $REPOSITORY" >&2
    exit 2
fi

DEADLINE=$((SECONDS + WATCH_TIMEOUT_MINUTES * 60))
PREVIOUS_STATUS=""
printf 'Watching run %s in %s (status changes only).\n' "$RUN_ID" "$REPOSITORY"

while true; do
    STATE="$(gh run view "$RUN_ID" --repo "$REPOSITORY" --json status,conclusion --jq '[.status, .conclusion] | @tsv')"
    STATUS="${STATE%%$'\t'*}"
    CONCLUSION="${STATE#*$'\t'}"

    if [ "$STATUS" != "$PREVIOUS_STATUS" ]; then
        if [ "$STATUS" = "completed" ]; then
            printf 'Status: completed (%s).\n' "$CONCLUSION"
        else
            printf 'Status: %s.\n' "$STATUS"
        fi
        PREVIOUS_STATUS="$STATUS"
    fi

    if [ "$STATUS" = "completed" ]; then
        exit 0
    fi
    if [ "$SECONDS" -ge "$DEADLINE" ]; then
        echo "Run ${RUN_ID} is still ${STATUS} after ${WATCH_TIMEOUT_MINUTES} minutes." >&2
        exit 124
    fi
    sleep "$POLL_INTERVAL_SECONDS"
done
