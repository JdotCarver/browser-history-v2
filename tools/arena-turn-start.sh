#!/usr/bin/env bash
# tools/arena-turn-start.sh
#
# Arena turn-start sync + harness repair for the Browser History V2 plugin repository.
#
# Run at the START of every turn:   bash tools/arena-turn-start.sh
#
# Why this exists
# ---------------
# The Arena harness occasionally restores this checkout from a snapshot instead of
# leaving it in place. Two symptoms show up:
#   1. HEAD is reset to the base commit, so the branch looks "behind".
#   2. The working tree keeps changing after the first glance: restored files land
#      in WAVES, minutes apart, appearing and vanishing.
# Both are handled below. Nothing here touches the remote.
#
# What it does
#   1. Waits out an in-flight snapshot restore (the tree churns while it lands).
#   2. Fetches the session branch and classifies the local state:
#        - clean + in sync          -> nothing to do
#        - no remote branch yet     -> INFO only (first turn; never fetch-fail)
#        - local BEHIND             -> fast-forward (the user pushes between turns)
#        - local AHEAD              -> LEAVE HEAD ALONE (unpushed mid-turn work)
#        - HEAD == base commit      -> RE-CLONE: hard-reset to the remote tip
#        - both sides diverged      -> stop; repair by hand (docs/AGENT_NOTES.md)
#   3. Settles: requires the status hash to hold steady before trusting the tree.
#   4. Verifies the tree is whole (key files present, manifest.json parses).
#
# HISTORY / READ THIS BEFORE EDITING
# ----------------------------------
# An earlier version of this script (in a previous repository) classified with
# `reset --hard` whenever HEAD != remote, which would have eaten unpushed commits
# made mid-turn. The ancestry checks below are the fix; do not simplify them away.
#
# The first version here also died on a missing tracking ref (`git rev-parse
# FETCH_HEAD` aborts under `set -e`) on the very first turn, before the session
# branch existed on the remote. Hence the explicit "no remote branch yet" branch.
#
# CHICKEN-AND-EGG: after a re-clone this file itself may be missing until the
# snapshot restore settles. If it is absent, repair by hand using docs/AGENT_NOTES.md.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="arena/b80febd6-browser-history-v2"
BASE_COMMIT="7d28c21a45101f3d75991c1de97686eaad2c85b9"

# Files whose absence means "the snapshot restore is incomplete".
# Keep this in sync with docs/AGENT_NOTES.md §2 when load-bearing files appear.
KEY_FILES=(
    "plugin.json"
    "run.py"
    "requirements.txt"
    "SettingsTemplate.yaml"
    "plugin/main.py"
    "plugin/browsers.py"
)

cd "$REPO_ROOT" || exit 1

echo "== arena turn-start ($(date +%H:%M:%S)) =="
echo "   branch=$(git rev-parse --abbrev-ref HEAD)  HEAD=$(git rev-parse --short HEAD)  dirty=$(git status --porcelain | wc -l)"

# --- 1. Is a snapshot restore still landing files? ---------------------------
# Runs BEFORE the branch guard on purpose: while files are still landing, a "dirty"
# reading (or a half-restored branch) is an artifact of the restore, not a real
# reason to refuse to do anything.
status_hash() { git status --porcelain | md5sum; }

before="$(status_hash)"
sleep 3
if [ "$before" != "$(status_hash)" ]; then
    echo "!! working tree still changing (snapshot restore in flight) — waiting 10s"
    sleep 10
fi

# --- 2. Guard: the session branch must be checked out. -----------------------
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo "!! on '$CURRENT_BRANCH' but this session must run on '$BRANCH'"
    if [ -z "$(git status --porcelain)" ]; then
        echo "   -> switching back to the session branch (tree is clean)"
        git checkout "$BRANCH"
    else
        echo "   -> tree is dirty; NOT switching automatically. Resolve by hand:"
        echo "        git status --porcelain    # inspect"
        echo "        git stash                 # park it, then: git checkout $BRANCH"
        exit 1
    fi
fi

# --- 3. Fetch and classify. NEVER move HEAD when local is ahead. -------------
git fetch origin --quiet 2>/dev/null || echo "!! fetch failed (offline?) — classifying against the last known state"

if ! git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "== no remote branch '$BRANCH' yet (first turn, or the user has not pushed)"
    REMOTE=""
else
    REMOTE="$(git rev-parse --verify "origin/$BRANCH" 2>/dev/null || echo "")"
fi

# A depth-1 harness re-clone truncates BOTH sides at one commit each, so
# `merge-base --is-ancestor` fails in both directions and the classifier below
# would false-alarm "DIVERGED". Unshallow first, then classify against real history.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
    echo "   shallow clone detected — unshallowing before classifying"
    git fetch --unshallow origin --quiet 2>/dev/null || true
    if [ -n "$REMOTE" ]; then
        REMOTE="$(git rev-parse --verify "origin/$BRANCH" 2>/dev/null || echo "")"
    fi
fi

HEAD_NOW="$(git rev-parse HEAD)"
DIRTY="$(git status --porcelain)"

if [ -z "$REMOTE" ]; then
    echo "   local only: HEAD=$(git rev-parse --short HEAD) — nothing to sync against"
elif [ "$HEAD_NOW" = "$REMOTE" ] && [ -z "$DIRTY" ]; then
    echo "== clean and in sync with origin/$BRANCH ($(git rev-parse --short HEAD))"
elif [ "$HEAD_NOW" = "$REMOTE" ]; then
    # At the tip with a dirty tree: in-progress work or a stale snapshot, never a
    # reason to reset. The integrity check below reports anything actually broken.
    echo "!! HEAD at remote tip but tree dirty — leaving uncommitted work ALONE"
elif git merge-base --is-ancestor "$REMOTE" "$HEAD_NOW"; then
    echo "== local is AHEAD of origin/$BRANCH (unpushed commits) — leaving HEAD alone"
elif git merge-base --is-ancestor "$HEAD_NOW" "$REMOTE"; then
    if [ "$HEAD_NOW" = "$BASE_COMMIT" ]; then
        echo "!! RE-CLONE DETECTED (HEAD == base commit) — hard-resetting to the remote tip"
    else
        echo "== behind origin/$BRANCH (user pushed between turns) — fast-forwarding"
    fi
    git reset --hard "$REMOTE" >/dev/null
    # A hard reset restores tracked files only. Anything listed here is a leftover
    # from the disturbed checkout (or the user's mid-turn scribbles) — worth seeing
    # rather than silently dragging along.
    leftovers="$(git status --porcelain)"
    if [ -n "$leftovers" ]; then
        echo "   note: untracked/leftover entries after the reset:"
        echo "$leftovers" | sed 's/^/     /'
    fi
else
    echo "!! DIVERGED (both sides have unique commits) — repair by hand"
    echo "   see docs/AGENT_NOTES.md §2; do NOT force-push over the user's work"
    # The usual cause is a re-clone arriving mid-turn (observed twice on 2026-09-29,
    # both times right after a user-facing pause): HEAD is knocked back to the base
    # commit, the next commit therefore sits on top of BASE, and the push is rejected
    # because the remote still holds the earlier commits from this same session.
    # The re-parenting recipe below is the safe repair: it keeps the local tree
    # verbatim and makes it a child of the remote tip. Verify the tree first!
    echo "   if this session's local commit was rebuilt on top of $BASE_COMMIT"
    echo "   while the remote holds your earlier commits, re-parent it (nothing is lost):"
    echo "        git diff --stat origin/$BRANCH    # confirm only your intended changes"
    echo "        git reset --soft origin/$BRANCH"
    echo "        git commit -C HEAD@{1}            # re-use the previous message"
    exit 1
fi

# --- 4. Settle: restored files can keep arriving in waves. -------------------
for round in 1 2 3; do
    before="$(status_hash)"
    sleep 3
    if [ "$before" = "$(status_hash)" ]; then
        break
    fi
    echo "!! worktree still churning (restore wave $round) — waiting"
    sleep 5
done

# --- 5. Verify the tree is whole. -------------------------------------------
sleep 2
missing=0
for file in "${KEY_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "!! MISSING: $file"
        missing=1
    fi
done

# Any JSON we ship must at least parse; a corrupted manifest is invisible otherwise.
if [ -f plugin.json ]; then
    if ! python3 -c 'import json,sys; json.load(open("plugin.json"))'; then
        echo "!! plugin.json is not valid JSON"
        missing=1
    fi
fi

if [ "$missing" -ne 0 ]; then
    echo "!! tree incomplete — repair by hand (docs/AGENT_NOTES.md §2)"
    exit 1
fi

echo "== ready: HEAD=$(git rev-parse --short HEAD) dirty=$(git status --porcelain | wc -l)"
git log --oneline -3
